"""Bot for media downloader"""

import asyncio
import os
from datetime import datetime
from typing import Callable, List, Union

import pyrogram
from loguru import logger
from pyrogram import types
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from ruamel import yaml

from tg_media_archiver import utils
from tg_media_archiver.module.app import (
    Application,
    ChatDownloadConfig,
    ForwardStatus,
    QueryHandler,
    QueryHandlerStr,
    TaskNode,
    TaskType,
    UploadStatus,
)
from tg_media_archiver.module.filter import Filter
from tg_media_archiver.module.get_chat_history_v2 import get_chat_history_v2
from tg_media_archiver.module.language import Language, _t
from tg_media_archiver.module.manual_progress import ManualProgress
from tg_media_archiver.module.persistent_queue import PersistentQueue
from tg_media_archiver.module.queue_effects import QueueEffects
from tg_media_archiver.module.pyrogram_extension import (
    check_user_permission,
    get_utf16_length,
    parse_link,
    proc_cache_forward,
    report_bot_forward_status,
    report_bot_status,
    retry,
    set_meta_data,
    upload_telegram_chat_message,
)
from tg_media_archiver.utils.format import replace_date_time, validate_title
from tg_media_archiver.utils.meta_data import MetaData

# pylint: disable = C0301, R0902


class DownloadBot:
    """Download bot"""

    def __init__(self):
        self.bot = None
        self.client = None
        self.add_download_task: Callable = None
        self.download_chat_task: Callable = None
        self.app = None
        self.listen_forward_chat: dict = {}
        self.config: dict = {}
        self._yaml = yaml.YAML()
        self.config_path = os.path.join(os.path.abspath("."), "bot.yaml")
        self.download_command: dict = {}
        self.filter = Filter()
        self.bot_info = None
        self.task_node: dict = {}
        self.is_running = True
        self.allowed_user_ids: List[Union[int, str]] = []
        self.monitor_task = None

        meta = MetaData(datetime(2022, 8, 5, 14, 35, 12), 0, "", 0, 0, 0, "", 0)
        self.filter.set_meta_data(meta)

        self.download_filter: List[str] = []
        self.task_id: int = 0
        self.reply_task = None
        self.manual_progress = None
        self.persistent_queue = None
        self.queue_effects = None
        self.effects_task = None

    def gen_task_id(self) -> int:
        """Gen task id"""
        self.task_id += 1
        return self.task_id

    def add_task_node(self, node: TaskNode):
        """Add task node"""
        self.task_node[node.task_id] = node

    def remove_task_node(self, task_id: int):
        """Remove task node"""
        self.task_node.pop(task_id)

    def stop_task(self, task_id: str):
        """Stop task"""
        if task_id == "all":
            for value in self.task_node.values():
                value.stop_transmission()
        else:
            try:
                task = self.task_node.get(int(task_id))
                if task:
                    task.stop_transmission()
            except Exception:
                return

    async def update_reply_message(self):
        """Update reply message"""
        while self.is_running:
            for key, value in self.task_node.copy().items():
                if value.is_running:
                    await report_bot_status(self.bot, value)

            for key, value in self.task_node.copy().items():
                if value.is_running and value.is_finish():
                    self.remove_task_node(key)
            await asyncio.sleep(3)

    def assign_config(self, _config: dict):
        """assign config from str.

        Parameters
        ----------
        _config: dict
            application config dict

        Returns
        -------
        bool
        """

        self.download_filter = _config.get("download_filter", self.download_filter)

        return True

    def update_config(self):
        """Update config from str."""
        self.config["download_filter"] = self.download_filter

        with open("d", "w", encoding="utf-8") as yaml_file:
            self._yaml.dump(self.config, yaml_file)

    async def start(
        self,
        app: Application,
        client: pyrogram.Client,
        add_download_task: Callable,
        download_chat_task: Callable,
    ):
        """Start bot"""
        self.bot = pyrogram.Client(
            app.application_name + "_bot",
            api_hash=app.api_hash,
            api_id=app.api_id,
            bot_token=app.bot_token,
            workdir=app.session_file_path,
            proxy=app.proxy,
            max_concurrent_transmissions=app.max_concurrent_transmissions,
        )

        # Command list
        commands = [
            types.BotCommand("help", _t("Help")),
            types.BotCommand(
                "get_info", _t("Get group and user info from message link")
            ),
            types.BotCommand(
                "download",
                _t(
                    "To download the video, use the method to directly enter /download to view"
                ),
            ),
            types.BotCommand(
                "forward",
                _t("Forward video, use the method to directly enter /forward to view"),
            ),
            types.BotCommand(
                "listen_forward",
                _t(
                    "Listen forward, use the method to directly enter /listen_forward to view"
                ),
            ),
            types.BotCommand(
                "add_filter",
                _t(
                    "Add download filter, use the method to directly enter /add_filter to view"
                ),
            ),
            types.BotCommand("set_language", _t("Set language")),
            types.BotCommand("stop", _t("Stop bot download or forward")),
        ]

        self.app = app
        self.client = client
        self.add_download_task = add_download_task
        self.download_chat_task = download_chat_task

        # load config
        if os.path.exists(self.config_path):
            with open(self.config_path, encoding="utf-8") as f:
                config = self._yaml.load(f.read())
                if config:
                    self.config = config
                    self.assign_config(self.config)

        await self.bot.start()

        self.bot_info = await self.bot.get_me()

        if not self.app.allowed_user_ids:
            raise ValueError(
                "allowed_user_ids must contain at least one numeric Telegram user ID"
            )
        allowed_ids = []
        for raw_id in self.app.allowed_user_ids:
            if (
                isinstance(raw_id, bool)
                or not str(raw_id).isdigit()
                or int(raw_id) <= 0
            ):
                raise ValueError(
                    "allowed_user_ids must contain only positive numeric Telegram user IDs"
                )
            allowed_ids.append(int(raw_id))
        self.allowed_user_ids = sorted(set(allowed_ids))
        self.manual_progress = ManualProgress(
            self.allowed_user_ids[0],
            state_path=os.path.join(app.log_file_path, "manual-progress-panel.json"),
        )
        os.makedirs(app.save_path, exist_ok=True)
        self.persistent_queue = PersistentQueue(os.path.join(app.save_path, "files.db"))
        await self.persistent_queue.initialize_lifecycle(self.allowed_user_ids[0])

        await self.bot.set_bot_commands(
            [
                types.BotCommand("start", _t("Show available commands")),
                types.BotCommand("help", _t("Show available commands")),
                types.BotCommand("progress", "查看或关闭下载进度"),
            ]
        )
        await self.recover_pending_downloads()
        await self.manual_progress.restore(
            self.bot, InlineKeyboardMarkup, InlineKeyboardButton, self.bot_info.id
        )
        self.bot.add_handler(
            MessageHandler(
                download_forward_media,
                filters=pyrogram.filters.private
                & (
                    pyrogram.filters.photo
                    | pyrogram.filters.video
                    | pyrogram.filters.document
                )
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        self.bot.add_handler(
            MessageHandler(
                help_command,
                filters=pyrogram.filters.private
                & pyrogram.filters.command(["start", "help"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        self.bot.add_handler(
            MessageHandler(
                progress_command,
                filters=pyrogram.filters.private
                & pyrogram.filters.command("progress")
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        self.bot.add_handler(
            CallbackQueryHandler(
                progress_callback, filters=pyrogram.filters.regex(r"^mp:off:[0-9]+$")
            )
        )
        self.bot.add_handler(
            MessageHandler(
                progress_new_message,
                filters=pyrogram.filters.private
                & pyrogram.filters.incoming
                & pyrogram.filters.user(self.allowed_user_ids),
            ),
            group=1,
        )
        self.queue_effects = QueueEffects(
            self.persistent_queue, self.bot, self.allowed_user_ids[0]
        )
        self.effects_task = asyncio.create_task(self.queue_effects.run())

        self.reply_task = _bot.app.loop.create_task(_bot.update_reply_message())

    async def recover_pending_downloads(self):
        """Recreate in-memory work from the durable Bot queue after restart."""
        rows = await self.persistent_queue.list_pending(recover_running=True)
        by_user = {}
        for row in rows:
            user_id = int(row["user_id"])
            chat_id = int(row["chat_id"])
            message_id = int(row["message_id"])
            if user_id not in self.allowed_user_ids or chat_id != user_id:
                await self.persistent_queue.mark_failed(
                    chat_id, message_id, "UnauthorizedRecord"
                )
                logger.warning(
                    "Bot recovery rejected unauthorized record: chat={} id={}",
                    chat_id,
                    message_id,
                )
                continue
            by_user.setdefault(user_id, []).append(message_id)

        for user_id, ids in by_user.items():
            for offset in range(0, len(ids), 100):
                batch = ids[offset : offset + 100]
                try:
                    found = await self.bot.get_messages(user_id, batch, replies=0)
                    messages = found if isinstance(found, list) else [found]
                except Exception as exc:
                    logger.warning(
                        "Bot recovery batch failed for user={} type={}; retrying individually",
                        user_id,
                        type(exc).__name__,
                    )
                    messages = []
                    for message_id in batch:
                        try:
                            messages.append(
                                await self.bot.get_messages(
                                    user_id, message_id, replies=0
                                )
                            )
                        except Exception as item_exc:
                            logger.warning(
                                "Bot recovery message={} type={}",
                                message_id,
                                type(item_exc).__name__,
                            )
                valid_ids = set(batch)
                found_ids = set()
                for message in messages:
                    if message is None or getattr(message, "empty", False):
                        continue
                    message_id = getattr(message, "id", None)
                    if message_id not in valid_ids:
                        continue
                    found_ids.add(message_id)
                    if not _is_authorized_forward(message, self.allowed_user_ids):
                        await self.persistent_queue.mark_failed(
                            user_id, message_id, "InvalidForward"
                        )
                        logger.warning(
                            "Bot recovery rejected non-forward media: user={} id={}",
                            user_id,
                            message_id,
                        )
                        continue
                    await direct_download(
                        self, user_id, message, message, self.bot, recovered=True
                    )
                for message_id in valid_ids - found_ids:
                    await self.persistent_queue.mark_failed(
                        user_id, message_id, "MissingMessage"
                    )
                    logger.warning(
                        "Bot recovery missing/deleted message: user={} id={}",
                        user_id,
                        message_id,
                    )


_bot = DownloadBot()


async def start_download_bot(
    app: Application,
    client: pyrogram.Client,
    add_download_task: Callable,
    download_chat_task: Callable,
):
    """Start download bot"""
    await _bot.start(app, client, add_download_task, download_chat_task)


async def stop_download_bot():
    """Stop download bot"""
    _bot.update_config()
    _bot.is_running = False
    if _bot.effects_task:
        _bot.effects_task.cancel()
    if _bot.reply_task:
        _bot.reply_task.cancel()
    if _bot.manual_progress and _bot.manual_progress.loop_task:
        _bot.manual_progress.loop_task.cancel()
    _bot.stop_task("all")
    if _bot.bot:
        await _bot.bot.stop()
    if _bot.monitor_task:
        _bot.monitor_task.cancel()
        _bot.monitor_task = None


async def send_help_str(client: pyrogram.Client, chat_id):
    """Send the restricted private Bot command guide."""
    await client.send_message(
        chat_id,
        "🤖 Telegram 媒体下载机器人\n"
        "/start — 查看使用说明\n"
        "/help — 查看使用说明\n"
        "/progress — 打开或刷新下载进度\n"
        "/progress off — 删除进度面板并停止更新\n\n"
        "仅接收授权用户在私聊中转发的图片、视频和文件。"
        "下载内容按月份保存到已配置目录。成功保存或确认重复后，双方删除对应转发消息。"
        "不发送单文件进度；多项队列全部成功处理后发送一次完成通知。",
    )


async def help_command(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Sends a message with the available commands and their usage.

    Parameters:
        client (pyrogram.Client): The client instance.
        message (pyrogram.types.Message): The message object.

    Returns:
        None
    """

    await send_help_str(client, message.chat.id)


async def progress_command(client: pyrogram.Client, message: pyrogram.types.Message):
    """Show or stop the single private progress panel."""
    if (
        not _bot.manual_progress
        or not message.from_user
        or message.from_user.id != _bot.manual_progress.owner_id
        or not message.chat
        or message.chat.type != pyrogram.enums.ChatType.PRIVATE
    ):
        return
    args = (message.text or "").strip().split()
    if len(args) > 1 and args[1].lower() == "off":
        await _bot.manual_progress.close(client)
    else:
        await _bot.manual_progress.open(
            client, InlineKeyboardMarkup, InlineKeyboardButton
        )


async def progress_callback(
    client: pyrogram.Client, query: pyrogram.types.CallbackQuery
):
    """Only the owner and current private panel may close updates."""
    if _bot.manual_progress:
        await _bot.manual_progress.callback(client, query)


async def progress_new_message(
    client: pyrogram.Client, message: pyrogram.types.Message
):
    """Keep an already-open panel after the owner's new private content."""
    if _bot.manual_progress:
        await _bot.manual_progress.observe_message(
            client, message, InlineKeyboardMarkup, InlineKeyboardButton
        )


async def set_language(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Set the language of the bot.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the command.

    Returns:
        None
    """

    if len(message.text.split()) != 2:
        await client.send_message(
            message.from_user.id,
            _t("Invalid command format. Please use /set_language en/ru/zh/ua"),
        )
        return

    language = message.text.split()[1]

    try:
        language = Language[language.upper()]
        _bot.app.set_language(language)
        await client.send_message(
            message.from_user.id, f"{_t('Language set to')} {language.name}"
        )
    except KeyError:
        await client.send_message(
            message.from_user.id,
            _t("Invalid command format. Please use /set_language en/ru/zh/ua"),
        )


async def get_info(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Async function that retrieves information from a group message link.
    """

    msg = _t("Invalid command format. Please use /get_info group_message_link")

    args = message.text.split()
    if len(args) != 2:
        await client.send_message(
            message.from_user.id,
            msg,
        )
        return

    chat_id, message_id, _ = await parse_link(_bot.client, args[1])

    entity = None
    if chat_id:
        entity = await _bot.client.get_chat(chat_id)

    if entity:
        if message_id:
            _message = await retry(_bot.client.get_messages, args=(chat_id, message_id))
            if _message:
                meta_data = MetaData()
                set_meta_data(meta_data, _message)
                msg = (
                    f"`\n"
                    f"{_t('Group/Channel')}\n"
                    f"├─ {_t('id')}: {entity.id}\n"
                    f"├─ {_t('first name')}: {entity.first_name}\n"
                    f"├─ {_t('last name')}: {entity.last_name}\n"
                    f"└─ {_t('name')}: {entity.username}\n"
                    f"{_t('Message')}\n"
                )

                for key, value in meta_data.data().items():
                    if key == "send_name":
                        msg += f"└─ {key}: {value or None}\n"
                    else:
                        msg += f"├─ {key}: {value or None}\n"

                msg += "`"
    await client.send_message(
        message.from_user.id,
        msg,
    )


async def add_filter(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Set the download filter of the bot.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the command.

    Returns:
        None
    """

    args = message.text.split(maxsplit=1)
    if len(args) != 2:
        await client.send_message(
            message.from_user.id,
            _t("Invalid command format. Please use /add_filter your filter"),
        )
        return

    filter_str = replace_date_time(args[1])
    res, err = _bot.filter.check_filter(filter_str)
    if res:
        _bot.app.down = args[1]
        await client.send_message(
            message.from_user.id, f"{_t('Add download filter')} : {args[1]}"
        )
    else:
        await client.send_message(
            message.from_user.id, f"{err}\n{_t('Check error, please add again!')}"
        )
    return


async def add_filter_advertisement_filter(
    client: pyrogram.Client, message: pyrogram.types.Message
):
    """
    Set the download filter of the bot.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the command.

    Returns:
        None
    """

    args = message.text.split(maxsplit=1)
    if len(args) != 2:
        await client.send_message(
            message.from_user.id,
            _t("Invalid command format. Please use /add_ad filter"),
        )
        return

    filter_str = args[1]

    _bot.app.filter_advertisement_list.append(filter_str)
    await client.send_message(message.from_user.id, f"{_t('Add filter')} : {args[1]}")
    _bot.app.update_config(True)


async def remove_filter_advertisement_filter(
    client: pyrogram.Client, message: pyrogram.types.Message
):
    """
    Add or remove advertisement filter
    """

    args = message.text.split(maxsplit=1)
    if len(args) != 2:
        await client.send_message(
            message.from_user.id,
            _t("Invalid command format. Please use /remove_ad filter"),
        )
        return

    filter_str = args[1]
    if filter_str in _bot.app.filter_advertisement_list:
        _bot.app.filter_advertisement_list.remove(filter_str)
        await client.send_message(
            message.from_user.id, f"{_t('Remove filter')} : {args[1]}"
        )

        _bot.app.update_config(True)
    else:
        await client.send_message(
            message.from_user.id, f"{_t('Filter')} : {args[1]} {_t('not exist')}"
        )


async def set_add_advertisement(
    client: pyrogram.Client, message: pyrogram.types.Message
):
    """
    Add or remove advertisement filter
    """

    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        await client.send_message(
            message.from_user.id,
            _t("Invalid command format. Please use /set_ad mesage_link advertisement"),
        )
        return

    mesage_link = args[1]
    advertisement_str = None if len(args) < 3 else args[2]

    try:
        chat_id, _, _ = await parse_link(_bot.client, mesage_link)
        _bot.app.group_add_advertisement[chat_id] = advertisement_str
        _bot.app.update_config(True)
        await client.send_message(
            message.from_user.id, f"{_t('Set advertisement')} : {advertisement_str}"
        )
    except Exception as e:
        await client.send_message(
            message.from_user.id, f"{_t('Parse link error')}: {e}"
        )
        return


class MessageProcessor:
    """Helper class for processing message captions and entities."""

    def __init__(self, raw_message, filter_str):
        self.raw_message = raw_message
        self.raw_caption = raw_message.caption
        self.filter_str = filter_str
        self.raw_filter_str = pyrogram.parser.utils.add_surrogates(filter_str)
        self.raw_caption_str = pyrogram.parser.utils.add_surrogates(raw_message.caption)
        self.idx = self.raw_caption_str.find(self.raw_filter_str)
        self.start_offset = self.idx
        self.end_offset = self.idx + get_utf16_length(filter_str)
        self.filtered_entities = []

    # pylint: disable = R0916
    def process_entities(self):
        """Process and filter message entities."""
        for entity in self.raw_message.caption_entities:
            cur_start_offset = entity.offset
            cur_end_offset = entity.offset + entity.length

            # Check if entity should be included
            if (
                (
                    cur_start_offset >= self.start_offset
                    and cur_end_offset <= self.end_offset
                )
                or (
                    cur_start_offset < self.start_offset
                    and cur_end_offset > self.start_offset
                )
                or (
                    cur_start_offset < self.end_offset
                    and cur_end_offset > self.end_offset
                )
            ):
                self.filtered_entities.append(entity)

        self.filtered_entities.sort(key=lambda x: x.offset)

    def get_total_span(self):
        """Calculate the total span for text extraction."""
        if self.filtered_entities:
            first_entity = self.filtered_entities[0]
            last_entity = self.filtered_entities[-1]
            return (
                min(self.start_offset, first_entity.offset),
                max(self.end_offset, last_entity.offset + last_entity.length),
            )
        return (self.start_offset, self.end_offset)

    def extract_text(self, total_span):
        """Extract and process text with adjusted entity offsets."""
        text = self.raw_caption[total_span[0] : total_span[1]]
        for entity in self.filtered_entities:
            entity.offset -= total_span[0]
        return pyrogram.parser.Parser.unparse(text, self.filtered_entities, True)


async def proc_replace_advertisement(mesage_link: str, filter_str: str):
    """
    Process and replace advertisement content in a message.

    This function takes a message link and a filter string, retrieves the message,
    and processes its caption by handling entities and filtering advertisement content.
    It preserves the formatting and entities while replacing the specified filter text.

    Args:
        mesage_link (str): The link to the Telegram message
        filter_str (str): The string to filter/replace in the message caption

    Returns:
        str: The processed caption with preserved formatting and entities

    Raises:
        Exception: If there are issues parsing the message link or accessing the message
    """
    chat_id, message_id, _ = await parse_link(_bot.client, mesage_link)
    raw_message = await retry(_bot.client.get_messages, args=(chat_id, message_id))

    processor = MessageProcessor(raw_message, filter_str)
    processor.process_entities()
    total_span = processor.get_total_span()
    return processor.extract_text(total_span)


async def add_replace_advertisement_filter(
    client: pyrogram.Client, message: pyrogram.types.Message
):
    """
    Set the download filter of the bot.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the command.

    Returns:
        None
    """

    args = message.text.split(maxsplit=2)
    if len(args) != 3:
        await client.send_message(
            message.from_user.id,
            _t("Invalid command format. Please use /add_replace_ad your filter"),
        )
        return

    mesage_link = args[1]
    filter_str = args[2]

    try:
        filter_str = await proc_replace_advertisement(mesage_link, filter_str)
        _bot.app.replace_advertisement_list.append(filter_str)
        _bot.app.update_config(True)
        await client.send_message(
            message.from_user.id, f"{_t('Add filter')} : {filter_str}"
        )
    except Exception as e:
        await client.send_message(
            message.from_user.id, f"{_t('Add filter')} : {filter_str}\n{e}"
        )
        return


async def remove_replace_advertisement_filter(
    client: pyrogram.Client, message: pyrogram.types.Message
):
    """
    Set the download filter of the bot.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the command.

    Returns:
        None
    """

    args = message.text.split(maxsplit=2)
    if len(args) != 3:
        await client.send_message(
            message.from_user.id,
            _t(
                "Invalid command format. Please use /remove_replace_ad mesage_link advertisement_filter"
            ),
        )
        return

    mesage_link = args[1]
    filter_str = args[2]

    try:
        filter_str = await proc_replace_advertisement(mesage_link, filter_str)

        if filter_str in _bot.app.replace_advertisement_list:
            _bot.app.replace_advertisement_list.remove(filter_str)
            await client.send_message(
                message.from_user.id, f"{_t('Remove filter')} : {filter_str}"
            )
        else:
            _bot.app.replace_advertisement_list.append(filter_str)
            await client.send_message()
        _bot.app.update_config(True)
    except Exception as e:
        await client.send_message(
            message.from_user.id, f"{_t('Add filter')} : {filter_str}\n{e}"
        )
        return


async def direct_download(
    download_bot: DownloadBot,
    chat_id: Union[str, int],
    message: pyrogram.types.Message,
    download_message: pyrogram.types.Message,
    client: pyrogram.Client = None,
    recovered: bool = False,
):
    """Direct Download"""

    if not recovered:
        try:
            inserted = await download_bot.persistent_queue.enqueue(
                int(chat_id), download_message.id, message.from_user.id
            )
        except Exception:
            logger.exception(
                "Bot queue persistence failed: chat={} id={}",
                chat_id,
                download_message.id,
            )
            try:
                await download_bot.bot.send_message(
                    message.from_user.id,
                    "下载队列暂时无法保存，本条未入队。请稍后重新转发。",
                )
            except Exception:
                logger.warning("Bot queue failure notice could not be sent")
            return False
        if not inserted:
            return False

    node = TaskNode(
        chat_id=chat_id,
        from_user_id=message.from_user.id,
        reply_message_id=0,
        replay_message="",
        limit=1,
        bot=download_bot.bot,
        task_id=_bot.gen_task_id(),
    )

    node.client = client
    node._manual_started_ids = set()
    node._persistent_queue = download_bot.persistent_queue
    node._persistent_message_id = download_message.id

    _bot.add_task_node(node)
    try:
        accepted = await _bot.add_download_task(download_message, node)
        if not accepted:
            raise ValueError("EmptyMessage")
    except Exception as exc:
        _bot.remove_task_node(node.task_id)
        await download_bot.persistent_queue.mark_failed(
            int(chat_id), download_message.id, type(exc).__name__
        )
        logger.exception(
            "Bot enqueue failed: chat={} id={}", chat_id, download_message.id
        )
        return False

    node.is_running = True
    if _bot.manual_progress:
        _bot.manual_progress.track(node, download_message)
    return True


def _is_authorized_forward(message, allowed_user_ids):
    """Check both live updates and recovered Bot messages."""
    if (
        not message
        or not message.from_user
        or message.from_user.id not in allowed_user_ids
    ):
        return False
    if (
        not message.chat
        or message.chat.type != pyrogram.enums.ChatType.PRIVATE
        or message.chat.id != message.from_user.id
    ):
        return False
    if getattr(message, "forward_date", None) is None:
        return False
    media_type = getattr(getattr(message, "media", None), "value", None)
    return (
        media_type in ("photo", "video", "document")
        and getattr(message, media_type, None) is not None
    )


async def download_forward_media(
    client: pyrogram.Client, message: pyrogram.types.Message
):
    """
    Downloads the media from a forwarded message.

    Parameters:
        client (pyrogram.Client): The client instance.
        message (pyrogram.types.Message): The message object.

    Returns:
        None
    """

    if not _is_authorized_forward(message, _bot.allowed_user_ids):
        return
    await direct_download(_bot, message.from_user.id, message, message, client)


async def download_from_link(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Downloads a single message from a Telegram link.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the Telegram link.

    Returns:
        None
    """

    if not message.text or not message.text.startswith("https://t.me"):
        return

    msg = (
        f"1. {_t('Directly download a single message')}\n"
        "<i>https://t.me/12000000/1</i>\n\n"
    )

    text = message.text.split()
    if len(text) != 1:
        await client.send_message(
            message.from_user.id, msg, parse_mode=pyrogram.enums.ParseMode.HTML
        )

    chat_id, message_id, _ = await parse_link(_bot.client, text[0])

    entity = None
    if chat_id:
        entity = await _bot.client.get_chat(chat_id)
    if entity:
        if message_id:
            download_message = await retry(
                _bot.client.get_messages, args=(chat_id, message_id)
            )
            if download_message:
                await direct_download(_bot, entity.id, message, download_message)
            else:
                client.send_message(
                    message.from_user.id,
                    f"{_t('From')} {entity.title} {_t('download')} {message_id} {_t('error')}!",
                    reply_to_message_id=message.id,
                )
        return

    await client.send_message(
        message.from_user.id, msg, parse_mode=pyrogram.enums.ParseMode.HTML
    )


# pylint: disable = R0912, R0915,R0914


async def download_from_bot(client: pyrogram.Client, message: pyrogram.types.Message):
    """Download from bot"""

    msg = (
        f"{_t('Parameter error, please enter according to the reference format')}:\n\n"
        f"1. {_t('Download all messages of common group')}\n"
        "<i>/download https://t.me/fkdhlg 1 0</i>\n\n"
        f"{_t('The private group (channel) link is a random group message link')}\n\n"
        f"2. {_t('The download starts from the N message to the end of the M message')}. "
        f"{_t('When M is 0, it means the last message. The filter is optional')}\n"
        f"<i>/download https://t.me/12000000 N M [filter]</i>\n\n"
    )

    args = message.text.split(maxsplit=4)
    if not message.text or len(args) < 4:
        await client.send_message(
            message.from_user.id, msg, parse_mode=pyrogram.enums.ParseMode.HTML
        )
        return

    url = args[1]
    try:
        start_offset_id = int(args[2])
        end_offset_id = int(args[3])
    except Exception:
        await client.send_message(
            message.from_user.id, msg, parse_mode=pyrogram.enums.ParseMode.HTML
        )
        return

    limit = 0
    if end_offset_id:
        if end_offset_id < start_offset_id:
            raise ValueError(
                f"end_offset_id < start_offset_id, {end_offset_id} < {start_offset_id}"
            )

        limit = end_offset_id - start_offset_id + 1

    download_filter = args[4] if len(args) > 4 else None

    if download_filter:
        download_filter = replace_date_time(download_filter)
        res, err = _bot.filter.check_filter(download_filter)
        if not res:
            await client.send_message(
                message.from_user.id, err, reply_to_message_id=message.id
            )
            return
    try:
        chat_id, _, _ = await parse_link(_bot.client, url)
        if chat_id:
            entity = await _bot.client.get_chat(chat_id)
        if entity:
            chat_title = entity.title
            reply_message = f"from {chat_title} "
            chat_download_config = ChatDownloadConfig()
            chat_download_config.last_read_message_id = start_offset_id
            chat_download_config.download_filter = download_filter
            reply_message += (
                f"download message id = {start_offset_id} - {end_offset_id} !"
            )
            last_reply_message = await client.send_message(
                message.from_user.id, reply_message, reply_to_message_id=message.id
            )
            node = TaskNode(
                chat_id=entity.id,
                from_user_id=message.from_user.id,
                reply_message_id=last_reply_message.id,
                replay_message=reply_message,
                limit=limit,
                start_offset_id=start_offset_id,
                end_offset_id=end_offset_id,
                bot=_bot.bot,
                task_id=_bot.gen_task_id(),
            )
            _bot.add_task_node(node)
            _bot.app.loop.create_task(
                _bot.download_chat_task(_bot.client, chat_download_config, node)
            )
    except Exception as e:
        await client.send_message(
            message.from_user.id,
            f"{_t('chat input error, please enter the channel or group link')}\n\n"
            f"{_t('Error type')}: {e.__class__}"
            f"{_t('Exception message')}: {e}",
        )
        return


async def get_forward_task_node(
    client: pyrogram.Client,
    message: pyrogram.types.Message,
    task_type: TaskType,
    src_chat_link: str,
    dst_chat_link: str,
    offset_id: int = 0,
    end_offset_id: int = 0,
    download_filter: str = None,
    reply_comment: bool = False,
):
    """Get task node"""
    limit: int = 0

    if end_offset_id:
        if end_offset_id < offset_id:
            await client.send_message(
                message.from_user.id,
                f" end_offset_id({end_offset_id}) < start_offset_id({offset_id}),"
                f" end_offset_id{_t('must be greater than')} offset_id",
            )
            return None

        limit = end_offset_id - offset_id + 1

    src_chat_id, _, _ = await parse_link(_bot.client, src_chat_link)
    dst_chat_id, target_msg_id, topic_id = await parse_link(_bot.client, dst_chat_link)

    if not src_chat_id or not dst_chat_id:
        logger.info(f"{src_chat_id} {dst_chat_id}")
        await client.send_message(
            message.from_user.id,
            _t("Invalid chat link") + f"{src_chat_id} {dst_chat_id}",
            reply_to_message_id=message.id,
        )
        return None

    try:
        src_chat = await _bot.client.get_chat(src_chat_id)
        dst_chat = await _bot.client.get_chat(dst_chat_id)
    except Exception as e:
        await client.send_message(
            message.from_user.id,
            f"{_t('Invalid chat link')} {e}",
            reply_to_message_id=message.id,
        )
        logger.exception(f"get chat error: {e}")
        return None

    me = await client.get_me()
    if dst_chat.id == me.id:
        # TODO: when bot receive message judge if download
        await client.send_message(
            message.from_user.id,
            _t("Cannot be forwarded to this bot, will cause an infinite loop"),
            reply_to_message_id=message.id,
        )
        return None

    if download_filter:
        download_filter = replace_date_time(download_filter)
        res, err = _bot.filter.check_filter(download_filter)
        if not res:
            await client.send_message(
                message.from_user.id, err, reply_to_message_id=message.id
            )

    last_reply_message = await client.send_message(
        message.from_user.id,
        "Forwarding message, please wait...",
        reply_to_message_id=message.id,
    )

    node = TaskNode(
        chat_id=src_chat.id,
        from_user_id=message.from_user.id,
        upload_telegram_chat_id=dst_chat_id,
        reply_message_id=last_reply_message.id,
        replay_message=last_reply_message.text,
        has_protected_content=src_chat.has_protected_content,
        download_filter=download_filter,
        limit=limit,
        start_offset_id=offset_id,
        end_offset_id=end_offset_id,
        bot=_bot.bot,
        task_id=_bot.gen_task_id(),
        task_type=task_type,
        topic_id=topic_id,
    )

    if target_msg_id and reply_comment:
        node.reply_to_message = await _bot.client.get_discussion_message(
            dst_chat_id, target_msg_id
        )

    _bot.add_task_node(node)

    node.upload_user = _bot.client
    if not dst_chat.type is pyrogram.enums.ChatType.BOT:
        has_permission = await check_user_permission(_bot.client, me.id, dst_chat.id)
        if has_permission:
            node.upload_user = _bot.bot

    if node.upload_user is _bot.client:
        await client.edit_message_text(
            message.from_user.id,
            last_reply_message.id,
            "Note that the robot may not be in the target group,"
            " use the user account to forward",
        )

    return node


# pylint: disable = R0914
async def forward_message_impl(
    client: pyrogram.Client, message: pyrogram.types.Message, reply_comment: bool
):
    """
    Forward message
    """

    async def report_error(client: pyrogram.Client, message: pyrogram.types.Message):
        """Report error"""

        await client.send_message(
            message.from_user.id,
            f"{_t('Invalid command format')}."
            f"{_t('Please use')} "
            "/forward https://t.me/c/src_chat https://t.me/c/dst_chat "
            f"1 400 `[`{_t('Filter')}`]`\n",
        )

    args = message.text.split(maxsplit=5)
    if len(args) < 5:
        await report_error(client, message)
        return

    src_chat_link = args[1]
    dst_chat_link = args[2]

    try:
        offset_id = int(args[3])
        end_offset_id = int(args[4])
    except Exception:
        await report_error(client, message)
        return

    download_filter = args[5] if len(args) > 5 else None

    node = await get_forward_task_node(
        client,
        message,
        TaskType.Forward,
        src_chat_link,
        dst_chat_link,
        offset_id,
        end_offset_id,
        download_filter,
        reply_comment,
    )

    if not node:
        return

    if not node.has_protected_content:
        try:
            async for item in get_chat_history_v2(  # type: ignore
                _bot.client,
                node.chat_id,
                limit=node.limit,
                max_id=node.end_offset_id,
                offset_id=offset_id,
                reverse=True,
            ):
                await forward_normal_content(client, node, item)
                if node.is_stop_transmission:
                    await client.edit_message_text(
                        message.from_user.id,
                        node.reply_message_id,
                        f"{_t('Stop Forward')}",
                    )
                    break
        except Exception as e:
            await client.edit_message_text(
                message.from_user.id,
                node.reply_message_id,
                f"{_t('Error forwarding message')} {e}",
            )
        finally:
            await report_bot_status(client, node, immediate_reply=True)
            node.stop_transmission()
    else:
        await forward_msg(node, offset_id)


async def forward_messages(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Forwards messages from one chat to another.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the command.

    Returns:
        None
    """
    return await forward_message_impl(client, message, False)


async def forward_normal_content(
    client: pyrogram.Client, node: TaskNode, message: pyrogram.types.Message
):
    """Forward normal content"""
    forward_ret = ForwardStatus.FailedForward
    caption = message.caption
    if caption:
        caption = validate_title(caption)
        _bot.app.set_caption_name(node.chat_id, message.media_group_id, caption)
    else:
        caption = _bot.app.get_caption_name(node.chat_id, message.media_group_id)

    if caption and _bot.app.is_match_advertisement(caption):
        forward_ret = ForwardStatus.SkipForward
        if message.media_group_id:
            # TODO
            node.upload_status[message.id] = UploadStatus.SkipUpload
        return

    if node.download_filter:
        meta_data = MetaData()
        set_meta_data(meta_data, message, caption)
        _bot.filter.set_meta_data(meta_data)
        if not _bot.filter.exec(node.download_filter):
            forward_ret = ForwardStatus.SkipForward
            if message.media_group_id:
                node.upload_status[message.id] = UploadStatus.SkipUpload
                await proc_cache_forward(_bot.client, node, message, False, _bot.app)
            await report_bot_forward_status(client, node, forward_ret)
            return

    await upload_telegram_chat_message(
        _bot.client, node.upload_user, _bot.app, node, message
    )


async def forward_msg(node: TaskNode, message_id: int):
    """Forward normal message"""

    chat_download_config = ChatDownloadConfig()
    chat_download_config.last_read_message_id = message_id
    chat_download_config.download_filter = node.download_filter  # type: ignore

    await _bot.download_chat_task(_bot.client, chat_download_config, node)


async def check_new_messages(
    client: pyrogram.Client, chat_id: int, node: TaskNode, last_message_id: int = 0
):
    """
    Checks for new messages in the chat and forwards them.

    Parameters:
        client (pyrogram.Client): The pyrogram client
        chat_id (int): The chat ID to monitor
        node (TaskNode): The task node containing forwarding configuration
        last_message_id (int): The ID of the last processed message
    """
    try:
        # Only get the most recent message if last_message_id is 0
        if last_message_id == 0:
            async for message in get_chat_history_v2(  # type: ignore
                client,
                chat_id,
                limit=1,  # Get only the latest message
            ):
                last_message_id = message.id
                return last_message_id

        # Otherwise check for new messages after last_message_id
        async for message in get_chat_history_v2(  # type: ignore
            client, chat_id, limit=100, offset_id=last_message_id, reverse=True
        ):
            if message.id > last_message_id:
                if not node.has_protected_content:
                    await forward_normal_content(client, node, message)
                    await report_bot_status(client, node, immediate_reply=True)
                else:
                    await _bot.add_download_task(message, node)
                last_message_id = message.id
    except Exception as e:
        logger.exception(f"Error checking new messages in chat {chat_id}: {e}")

    return last_message_id


async def start_message_monitor():
    """
    Starts monitoring all chats that need to be forwarded.
    Runs every 60 seconds to check for new messages.
    """
    last_message_ids = {}  # 存储每个聊天的最后处理的消息ID

    while _bot.is_running:
        try:
            for chat_id, node in _bot.listen_forward_chat.items():
                if not node.is_running:
                    continue

                last_id = last_message_ids.get(chat_id, 0)
                new_last_id = await check_new_messages(
                    _bot.client, chat_id, node, last_id
                )
                last_message_ids[chat_id] = new_last_id

        except Exception as e:
            logger.exception(f"Error in message monitor: {e}")

        await asyncio.sleep(60)  # 每60秒检查一次


async def set_listen_forward_msg(
    client: pyrogram.Client, message: pyrogram.types.Message
):
    """
    Set the chat to listen for forwarded messages.
    """
    args = message.text.split(maxsplit=3)

    if len(args) < 3:
        await client.send_message(
            message.from_user.id,
            f"{_t('Invalid command format')}. {_t('Please use')} /listen_forward "
            f"https://t.me/c/src_chat https://t.me/c/dst_chat [{_t('Filter')}]\n",
        )
        return

    src_chat_link = args[1]
    dst_chat_link = args[2]
    download_filter = args[3] if len(args) > 3 else None

    node = await get_forward_task_node(
        client,
        message,
        TaskType.ListenForward,
        src_chat_link,
        dst_chat_link,
        download_filter=download_filter,
    )

    if not node:
        return

    if node.chat_id in _bot.listen_forward_chat:
        _bot.remove_task_node(_bot.listen_forward_chat[node.chat_id].task_id)

    node.is_running = True
    _bot.listen_forward_chat[node.chat_id] = node

    if not hasattr(_bot, "monitor_task") or _bot.monitor_task is None:
        _bot.monitor_task = _bot.app.loop.create_task(start_message_monitor())


async def stop(client: pyrogram.Client, message: pyrogram.types.Message):
    """Stops listening for forwarded messages."""

    await client.send_message(
        message.chat.id,
        _t("Please select:"),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        _t("Stop Download"), callback_data="stop_download"
                    ),
                    InlineKeyboardButton(
                        _t("Stop Forward"), callback_data="stop_forward"
                    ),
                ],
                [  # Second row
                    InlineKeyboardButton(
                        _t("Stop Listen Forward"), callback_data="stop_listen_forward"
                    )
                ],
            ]
        ),
    )


async def stop_task(
    client: pyrogram.Client,
    query: pyrogram.types.CallbackQuery,
    queryHandler: str,
    task_type: TaskType,
):
    """Stop task"""
    if query.data == queryHandler:
        buttons: List[InlineKeyboardButton] = []
        temp_buttons: List[InlineKeyboardButton] = []
        for key, value in _bot.task_node.copy().items():
            if not value.is_finish() and value.task_type is task_type:
                if len(temp_buttons) == 3:
                    buttons.append(temp_buttons)
                    temp_buttons = []
                temp_buttons.append(
                    InlineKeyboardButton(
                        f"{key}", callback_data=f"{queryHandler} task {key}"
                    )
                )
        if temp_buttons:
            buttons.append(temp_buttons)

        if buttons:
            buttons.insert(
                0,
                [
                    InlineKeyboardButton(
                        _t("all"), callback_data=f"{queryHandler} task all"
                    )
                ],
            )
            await client.edit_message_text(
                query.message.from_user.id,
                query.message.id,
                f"{_t('Stop')} {_t(task_type.name)}...",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        else:
            await client.edit_message_text(
                query.message.from_user.id,
                query.message.id,
                f"{_t('No Task')}",
            )
    else:
        task_id = query.data.split(" ")[2]
        await client.edit_message_text(
            query.message.from_user.id,
            query.message.id,
            f"{_t('Stop')} {_t(task_type.name)}...",
        )
        _bot.stop_task(task_id)


async def on_query_handler(
    client: pyrogram.Client, query: pyrogram.types.CallbackQuery
):
    """
    Asynchronous function that handles query callbacks.

    Parameters:
        client (pyrogram.Client): The Pyrogram client object.
        query (pyrogram.types.CallbackQuery): The callback query object.

    Returns:
        None
    """

    for it in QueryHandler:
        queryHandler = QueryHandlerStr.get_str(it.value)
        if queryHandler in query.data:
            await stop_task(client, query, queryHandler, TaskType(it.value))


async def forward_to_comments(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Forwards specified media to a designated comment section.

    Usage: /forward_to_comments <source_chat_link> <destination_chat_link> <msg_start_id> <msg_end_id>

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the command.
    """
    return await forward_message_impl(client, message, True)
