"""The owner's private Telegram bot and durable message intake."""

import asyncio
import logging
from pathlib import Path

from pyrogram import enums, filters, types
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from tg_media_archiver.module.app import DownloadStatus, TaskNode
from tg_media_archiver.module.manual_progress import ManualProgress
from tg_media_archiver.module.persistent_queue import PersistentQueue
from tg_media_archiver.module.queue_effects import QueueEffects
from tg_media_archiver.module.telegram import ArchiveClient

logger = logging.getLogger(__name__)
HELP_TEXT = (
    "🤖 Telegram 媒体归档机器人\n"
    "/start、/help — 查看说明\n"
    "/progress — 查看当前队列（10 秒后收起，可点击按钮再次查看）\n"
    "/progress off — 删除进度消息\n\n"
    "请在私聊中转发图片、视频或文件。保存或确认重复后，转发消息会为双方删除。"
)


def _is_authorized_forward(message, owner_id: int) -> bool:
    """Accept only forwarded media sent by the configured owner in private."""
    sender = getattr(message, "from_user", None)
    chat = getattr(message, "chat", None)
    media = getattr(message, "media", None)
    media_type = getattr(media, "value", None)
    return bool(
        sender
        and sender.id == owner_id
        and chat
        and chat.id == owner_id
        and chat.type == enums.ChatType.PRIVATE
        and getattr(message, "forward_date", None) is not None
        and media_type in ("photo", "video", "document")
        and getattr(message, media_type, None) is not None
    )


class DownloadBot:
    """Receive one owner's forwards and restore incomplete work at startup."""

    def __init__(self, app, enqueue_callback):
        self.app = app
        self.owner_id = app.allowed_user_ids[0]
        self.enqueue_callback = enqueue_callback
        self.client = None
        self.manual_progress = ManualProgress(
            self.owner_id,
            state_path=Path(app.log_file_path) / "manual-progress-panel.json",
        )
        self.persistent_queue = PersistentQueue(Path(app.save_path) / "files.db")
        self.queue_effects = None
        self.effects_task = None
        self._handlers = []
        self._next_task_id = 0
        self._startup_buffer = []
        self._accepting = False
        self._stopping = False
        self._scheduled_ids = set()

    def _add_handler(self, handler, *, group=0) -> None:
        self.client.add_handler(handler, group=group)
        self._handlers.append((handler, group))

    def _register_handlers(self) -> None:
        owner_private = filters.private & filters.user(self.owner_id)
        supported_media = filters.photo | filters.video | filters.document
        self._add_handler(
            MessageHandler(self._on_forward, owner_private & supported_media)
        )
        self._add_handler(
            MessageHandler(
                self._on_help,
                owner_private & filters.command(["start", "help"]),
            )
        )
        self._add_handler(
            MessageHandler(
                self._on_progress,
                owner_private & filters.command("progress"),
            )
        )
        self._add_handler(
            CallbackQueryHandler(
                self._on_progress_callback,
                filters.regex(r"^mp:(?:off|open):[0-9]+$"),
            )
        )
        self._add_handler(
            MessageHandler(
                self._on_activity,
                owner_private & filters.incoming,
            ),
            group=1,
        )

    async def start(self) -> None:
        """Start the bot, recover durable work, then accept live messages."""
        self.app.pre_run()
        await self.persistent_queue.initialize_lifecycle(self.owner_id)
        self.client = ArchiveClient(
            self.app.application_name + "_bot",
            api_id=self.app.api_id,
            api_hash=self.app.api_hash,
            bot_token=self.app.bot_token,
            workdir=self.app.session_file_path,
            proxy=self.app.proxy,
            max_concurrent_transmissions=self.app.max_concurrent_transmissions,
        )
        self._register_handlers()
        await asyncio.wait_for(self.client.start(), timeout=self.app.start_timeout)
        identity = await self.client.get_me()
        expected_id = int(self.app.bot_token.split(":", 1)[0])
        if not identity.is_bot or identity.id != expected_id:
            raise RuntimeError("Stored bot session does not match the configured token")
        try:
            await self.client.set_bot_commands(
                [
                    types.BotCommand("start", "查看使用说明"),
                    types.BotCommand("help", "查看使用说明"),
                    types.BotCommand("progress", "查看或关闭下载进度"),
                ]
            )
        except Exception as exc:
            logger.warning("Bot command menu unavailable: %s", type(exc).__name__)
        await self._recover_pending()
        await self.manual_progress.restore(
            self.client, InlineKeyboardMarkup, InlineKeyboardButton, identity.id
        )
        while self._startup_buffer:
            pending, self._startup_buffer = self._startup_buffer, []
            for message in pending:
                await self._enqueue_message(message, recovered=True)
        self._accepting = True
        self.queue_effects = QueueEffects(
            self.persistent_queue, self.client, self.owner_id
        )
        self.effects_task = asyncio.create_task(self.queue_effects.run())
        logger.info("Archive bot ready for owner %s", self.owner_id)

    async def stop_accepting(self) -> None:
        """Stop receiving updates while already queued workers settle."""
        self._accepting = False
        self._stopping = True
        for handler, group in self._handlers:
            self.client.remove_handler(handler, group=group)
        self._handlers.clear()

    async def stop(self) -> None:
        if self._handlers:
            await self.stop_accepting()
        if self.effects_task is not None:
            self.effects_task.cancel()
            await asyncio.gather(self.effects_task, return_exceptions=True)
        await self.manual_progress.shutdown()
        if self.client and self.client.is_connected:
            if self.client.is_initialized:
                await self.client.stop()
            else:
                await self.client.disconnect()

    async def _recover_pending(self) -> None:
        """Rebuild the in-memory queue without changing existing files.db rows."""
        rows = await self.persistent_queue.list_pending(recover_running=True)
        ids = []
        for row in rows:
            chat_id = int(row["chat_id"])
            message_id = int(row["message_id"])
            if chat_id != self.owner_id or int(row["user_id"]) != self.owner_id:
                await self.persistent_queue.mark_failed(
                    chat_id, message_id, "UnauthorizedRecord"
                )
                continue
            ids.append(message_id)

        for offset in range(0, len(ids), 100):
            batch = ids[offset : offset + 100]
            unavailable_ids = set()
            try:
                found = await self.client.get_messages(self.owner_id, batch, replies=0)
                messages = found if isinstance(found, list) else [found]
            except Exception as exc:
                logger.warning(
                    "Recovery batch unavailable: %s; trying individually",
                    type(exc).__name__,
                )
                messages = []
                for message_id in batch:
                    try:
                        messages.append(
                            await self.client.get_messages(
                                self.owner_id, message_id, replies=0
                            )
                        )
                    except Exception as item_exc:
                        unavailable_ids.add(message_id)
                        await self.persistent_queue.mark_failed(
                            self.owner_id, message_id, type(item_exc).__name__
                        )
                        logger.warning(
                            "Recovery message %s unavailable: %s",
                            message_id,
                            type(item_exc).__name__,
                        )
            found_ids = set()
            for message in messages:
                if message is None or getattr(message, "empty", False):
                    continue
                if getattr(message, "id", None) not in batch:
                    continue
                found_ids.add(message.id)
                if not _is_authorized_forward(message, self.owner_id):
                    await self.persistent_queue.mark_failed(
                        self.owner_id, message.id, "InvalidForward"
                    )
                    continue
                await self._enqueue_message(message, recovered=True)
            for message_id in set(batch) - found_ids - unavailable_ids:
                await self.persistent_queue.mark_failed(
                    self.owner_id, message_id, "MissingMessage"
                )

    async def _enqueue_message(self, message, *, recovered=False) -> bool:
        if not _is_authorized_forward(message, self.owner_id):
            return False
        if getattr(message.media, "value", None) not in self.app.media_types:
            if recovered:
                await self.persistent_queue.mark_failed(
                    self.owner_id, message.id, "UnsupportedMedia"
                )
            return False
        if message.id in self._scheduled_ids:
            return False
        if not recovered:
            try:
                inserted = await self.persistent_queue.enqueue(
                    self.owner_id, message.id, self.owner_id
                )
            except Exception as exc:
                logger.error("Queue persistence unavailable: %s", type(exc).__name__)
                return False
            if not inserted:
                return False
        if message.id in self._scheduled_ids:
            return False
        self._scheduled_ids.add(message.id)
        self._next_task_id += 1
        node = TaskNode(
            chat_id=self.owner_id,
            from_user_id=self.owner_id,
            task_id=self._next_task_id,
            client=self.client,
            _persistent_queue=self.persistent_queue,
        )
        node.download_status[message.id] = DownloadStatus.Downloading
        self.manual_progress.track(node, message)
        try:
            await self.enqueue_callback(message, node)
        except Exception as exc:
            self._scheduled_ids.discard(message.id)
            node.download_status[message.id] = DownloadStatus.FailedDownload
            await self.persistent_queue.mark_failed(
                self.owner_id, message.id, type(exc).__name__
            )
            logger.exception("Unable to enqueue message %s", message.id)
            return False
        return True

    async def _on_forward(self, _client, message) -> None:
        if not _is_authorized_forward(message, self.owner_id):
            return
        if message.media.value not in self.app.media_types:
            return
        if self._accepting:
            await self._enqueue_message(message)
        else:
            try:
                inserted = await self.persistent_queue.enqueue(
                    self.owner_id, message.id, self.owner_id
                )
                if inserted and not self._stopping:
                    self._startup_buffer.append(message)
            except Exception as exc:
                logger.error("Queue persistence unavailable: %s", type(exc).__name__)

    async def _on_help(self, client, message) -> None:
        if message.from_user and message.from_user.id == self.owner_id:
            await client.send_message(self.owner_id, HELP_TEXT)

    async def _on_progress(self, client, message) -> None:
        if not message.from_user or message.from_user.id != self.owner_id:
            return
        args = (message.text or "").strip().split()
        if len(args) > 1 and args[1].lower() == "off":
            await self.manual_progress.close(client)
        else:
            await self.manual_progress.open(
                client, InlineKeyboardMarkup, InlineKeyboardButton
            )

    async def _on_progress_callback(self, client, query) -> None:
        await self.manual_progress.callback(client, query)

    async def _on_activity(self, client, message) -> None:
        await self.manual_progress.observe_message(
            client, message, InlineKeyboardMarkup, InlineKeyboardButton
        )
