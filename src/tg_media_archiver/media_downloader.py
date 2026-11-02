"""Downloads media from telegram."""

import asyncio
import logging
import os
import shutil
import time
from typing import List, Optional, Tuple, Union

import pyrogram
from loguru import logger
from pyrogram.types import Audio, Document, Photo, Video, VideoNote, Voice
from rich.logging import RichHandler

from tg_media_archiver.module.app import (
    Application,
    ChatDownloadConfig,
    DownloadStatus,
    TaskNode,
)
from tg_media_archiver.module.bot import start_download_bot, stop_download_bot
from tg_media_archiver.module.download_stat import update_download_status
from tg_media_archiver.module.get_chat_history_v2 import get_chat_history_v2
from tg_media_archiver.module.language import _t
from tg_media_archiver.module.media_dedup import MediaDedup
from tg_media_archiver.module.pyrogram_extension import (
    HookClient,
    fetch_message,
    get_extension,
    record_download_status,
    report_bot_status,
    set_max_concurrent_transmissions,
    set_meta_data,
    update_cloud_upload_stat,
    upload_telegram_chat,
)
from tg_media_archiver.module.web import init_web
from tg_media_archiver.utils.format import truncate_filename, validate_title
from tg_media_archiver.utils.log import LogFilter
from tg_media_archiver.utils.meta import print_meta
from tg_media_archiver.utils.meta_data import MetaData

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler()],
)

CONFIG_NAME = "config.yaml"
DATA_FILE_NAME = "data.yaml"
APPLICATION_NAME = "media_downloader"
app = Application(CONFIG_NAME, DATA_FILE_NAME, APPLICATION_NAME)

queue: asyncio.Queue = asyncio.Queue()
RETRY_TIME_OUT = 3
_dedup_indexes = {}


def _dedup_index():
    """Keep one in-process index and lock registry per configured download root."""
    root = os.path.abspath(app.save_path)
    if root not in _dedup_indexes:
        _dedup_indexes[root] = MediaDedup(root)
    return _dedup_indexes[root]


logging.getLogger("pyrogram.session.session").addFilter(LogFilter())
logging.getLogger("pyrogram.client").addFilter(LogFilter())

logging.getLogger("pyrogram").setLevel(logging.WARNING)


def _check_download_finish(media_size: int, download_path: str, ui_file_name: str):
    """Check download task if finish

    Parameters
    ----------
    media_size: int
        The size of the downloaded resource
    download_path: str
        Resource download hold path
    ui_file_name: str
        Really show file name

    """
    download_size = os.path.getsize(download_path)
    if media_size == download_size:
        logger.success(f"{_t('Successfully downloaded')} - {ui_file_name}")
    else:
        logger.warning(
            f"{_t('Media downloaded with wrong size')}: "
            f"{download_size}, {_t('actual')}: "
            f"{media_size}, {_t('file name')}: {ui_file_name}"
        )
        os.remove(download_path)
        raise pyrogram.errors.exceptions.bad_request_400.BadRequest()


def _move_to_download_path(temp_download_path: str, download_path: str):
    """Publish without overwriting files or stripping the share's inherited ACL."""
    from tg_media_archiver.module.share_files import publish_download

    publish_download(temp_download_path, download_path, app.save_path)


def _check_timeout(retry: int, _: int):
    """Check if message download timeout, then add message id into failed_ids

    Parameters
    ----------
    retry: int
        Retry download message times

    message_id: int
        Try to download message 's id

    """
    if retry == 2:
        return True
    return False


def _can_download(_type: str, file_formats: dict, file_format: Optional[str]) -> bool:
    """
    Check if the given file format can be downloaded.

    Parameters
    ----------
    _type: str
        Type of media object.
    file_formats: dict
        Dictionary containing the list of file_formats
        to be downloaded for `audio`, `document` & `video`
        media types
    file_format: str
        Format of the current file to be downloaded.

    Returns
    -------
    bool
        True if the file format can be downloaded else False.
    """
    if _type in ["audio", "document", "video"]:
        allowed_formats: list = file_formats[_type]
        if not file_format in allowed_formats and allowed_formats[0] != "all":
            return False
    return True


def _is_exist(file_path: str) -> bool:
    """
    Check if a file exists and it is not a directory.

    Parameters
    ----------
    file_path: str
        Absolute path of the file to be checked.

    Returns
    -------
    bool
        True if the file exists else False.
    """
    return not os.path.isdir(file_path) and os.path.exists(file_path)


# pylint: disable = R0912


async def _get_media_meta(
    chat_id: Union[int, str],
    message: pyrogram.types.Message,
    media_obj: Union[Audio, Document, Photo, Video, VideoNote, Voice],
    _type: str,
) -> Tuple[str, str, Optional[str]]:
    """Extract file name and file id from media object.

    Parameters
    ----------
    media_obj: Union[Audio, Document, Photo, Video, VideoNote, Voice]
        Media object to be extracted.
    _type: str
        Type of media object.

    Returns
    -------
    Tuple[str, str, Optional[str]]
        file_name, file_format
    """
    if _type in ["audio", "document", "video"]:
        # pylint: disable = C0301
        file_format: Optional[str] = media_obj.mime_type.split("/")[-1]  # type: ignore
    else:
        file_format = None

    file_name = None
    temp_file_name = None
    dirname = validate_title(f"{chat_id}")
    if message.chat and message.chat.title:
        dirname = validate_title(f"{message.chat.title}")

    if message.date:
        datetime_dir_name = message.date.strftime(app.date_format)
    else:
        datetime_dir_name = "0"

    if _type in ["voice", "video_note"]:
        # pylint: disable = C0209
        file_format = media_obj.mime_type.split("/")[-1]  # type: ignore
        file_save_path = app.get_file_save_path(_type, dirname, datetime_dir_name)
        file_name = "{} - {}_{}.{}".format(
            message.id,
            _type,
            media_obj.date.isoformat(),  # type: ignore
            file_format,
        )
        file_name = validate_title(file_name)
        temp_file_name = os.path.join(app.temp_save_path, dirname, file_name)

        file_name = os.path.join(file_save_path, file_name)
    else:
        file_name = getattr(media_obj, "file_name", None)
        caption = getattr(message, "caption", None)

        file_name_suffix = ".unknown"
        if not file_name:
            file_name_suffix = get_extension(
                media_obj.file_id, getattr(media_obj, "mime_type", "")
            )
        else:
            # file_name = file_name.split(".")[0]
            _, file_name_without_suffix = os.path.split(os.path.normpath(file_name))
            file_name, file_name_suffix = os.path.splitext(file_name_without_suffix)
            if not file_name_suffix:
                file_name_suffix = get_extension(
                    media_obj.file_id, getattr(media_obj, "mime_type", "")
                )

        if caption:
            caption = validate_title(caption)
            app.set_caption_name(chat_id, message.media_group_id, caption)
            app.set_caption_entities(
                chat_id, message.media_group_id, message.caption_entities
            )
        else:
            caption = app.get_caption_name(chat_id, message.media_group_id)

        if not file_name and message.photo:
            file_name = f"{message.photo.file_unique_id}"

        gen_file_name = (
            app.get_file_name(message.id, file_name, caption) + file_name_suffix
        )

        file_save_path = app.get_file_save_path(_type, dirname, datetime_dir_name)

        temp_file_name = os.path.join(app.temp_save_path, dirname, gen_file_name)

        file_name = os.path.join(file_save_path, gen_file_name)
    return truncate_filename(file_name), truncate_filename(temp_file_name), file_format


async def add_download_task(
    message: pyrogram.types.Message,
    node: TaskNode,
):
    """Add Download task"""
    if message.empty:
        return False
    node.download_status[message.id] = DownloadStatus.Downloading
    await queue.put((message, node))
    node.total_task += 1
    return True


async def save_msg_to_file(
    app, chat_id: Union[int, str], message: pyrogram.types.Message
):
    """Write message text into file"""
    dirname = validate_title(
        message.chat.title if message.chat and message.chat.title else str(chat_id)
    )
    datetime_dir_name = message.date.strftime(app.date_format) if message.date else "0"

    file_save_path = app.get_file_save_path("msg", dirname, datetime_dir_name)
    file_name = os.path.join(
        app.temp_save_path,
        file_save_path,
        f"{app.get_file_name(message.id, None, None)}.txt",
    )

    os.makedirs(os.path.dirname(file_name), exist_ok=True)

    if _is_exist(file_name):
        return DownloadStatus.SkipDownload, None

    with open(file_name, "w", encoding="utf-8") as f:
        f.write(message.text or "")

    return DownloadStatus.SuccessDownload, file_name


async def download_task(
    client: pyrogram.Client, message: pyrogram.types.Message, node: TaskNode
):
    """Download, upload and account for every queued message exactly once."""
    download_status = DownloadStatus.FailedDownload
    failure_type = "DownloadFailed"
    file_name = None
    file_size = 0
    # A generic SkipDownload can mean an unsupported format, not a saved copy.
    node._verified_saved_ids = getattr(node, "_verified_saved_ids", set())
    node._verified_saved_ids.discard(message.id)
    try:
        download_status, file_name = await download_media(
            client, message, app.media_types, app.file_formats, node
        )

        if app.enable_download_txt and message.text and not message.media:
            download_status, file_name = await save_msg_to_file(
                app, node.chat_id, message
            )

        file_size = os.path.getsize(file_name) if file_name else 0
        await upload_telegram_chat(
            client,
            node.upload_user if node.upload_user else client,
            app,
            node,
            message,
            download_status,
            file_name,
        )

        if (
            not node.upload_telegram_chat_id
            and download_status is DownloadStatus.SuccessDownload
        ):
            ui_file_name = file_name
            if app.hide_file_name:
                ui_file_name = f"****{os.path.splitext(file_name)[-1]}"
            if await app.upload_file(
                file_name, update_cloud_upload_stat, (node, message.id, ui_file_name)
            ):
                node.upload_success_count += 1
    except asyncio.CancelledError:
        # The worker may be shutting down. Record a failed retryable task and
        # let cancellation propagate to the caller.
        download_status = DownloadStatus.FailedDownload
        failure_type = "CancelledError"
        raise
    except Exception as exc:
        logger.exception(f"Download task message_id={message.id} failed: {exc}")
        download_status = DownloadStatus.FailedDownload
        failure_type = type(exc).__name__
    finally:
        if getattr(node, "_persistent_queue", None) is not None and (
            (
                download_status is DownloadStatus.SkipDownload
                and message.id not in node._verified_saved_ids
            )
            or (
                download_status is DownloadStatus.SuccessDownload
                and (not file_name or not os.path.isfile(file_name))
            )
        ):
            download_status = DownloadStatus.FailedDownload
            failure_type = "UnsavedMedia"
        # Batch progress is tracked in ChatDownloadConfig; Bot progress is not.
        if not node.bot:
            app.set_download_id(node, message.id, download_status)
        node.download_status[message.id] = download_status
        node.stat(download_status)
        node.total_download_byte += file_size
        persistent_queue = getattr(node, "_persistent_queue", None)
        if persistent_queue is not None:
            try:
                if download_status in (
                    DownloadStatus.SuccessDownload,
                    DownloadStatus.SkipDownload,
                ):
                    await persistent_queue.mark_done(node.chat_id, message.id)
                else:
                    await persistent_queue.mark_failed(
                        node.chat_id, message.id, failure_type
                    )
            except Exception as exc:
                logger.warning(
                    "persistent task settlement unavailable: %s", type(exc).__name__
                )

    await report_bot_status(node.bot, node)


# pylint: disable = R0915,R0914


@record_download_status
async def download_media(
    client: pyrogram.client.Client,
    message: pyrogram.types.Message,
    media_types: List[str],
    file_formats: dict,
    node: TaskNode,
):
    """
    Download media from Telegram.

    Each of the files to download are retried 3 times with a
    delay of 5 seconds each.

    Parameters
    ----------
    client: pyrogram.client.Client
        Client to interact with Telegram APIs.
    message: pyrogram.types.Message
        Message object retrieved from telegram.
    media_types: list
        List of strings of media types to be downloaded.
        Ex : `["audio", "photo"]`
        Supported formats:
            * audio
            * document
            * photo
            * video
            * voice
    file_formats: dict
        Dictionary containing the list of file_formats
        to be downloaded for `audio`, `document` & `video`
        media types.

    Returns
    -------
    int
        Current message id.
    """

    # pylint: disable = R0912

    file_name: str = ""
    ui_file_name: str = ""
    task_start_time: float = time.time()
    media_size = 0
    _media = None
    message = await fetch_message(client, message)
    try:
        for _type in media_types:
            _media = getattr(message, _type, None)
            if _media is None:
                continue
            file_name, temp_file_name, file_format = await _get_media_meta(
                node.chat_id, message, _media, _type
            )
            media_size = getattr(_media, "file_size", 0)

            ui_file_name = file_name
            if app.hide_file_name:
                ui_file_name = f"****{os.path.splitext(file_name)[-1]}"

            if not _can_download(_type, file_formats, file_format):
                return DownloadStatus.SkipDownload, None

            break
    except Exception as e:
        logger.error(
            f"Message[{message.id}]: "
            f"{_t('could not be downloaded due to following exception')}:\n[{e}].",
            exc_info=True,
        )
        return DownloadStatus.FailedDownload, None
    if _media is None:
        return DownloadStatus.SkipDownload, None

    dedup = _dedup_index()
    identity = dedup.identity(
        _type, getattr(_media, "file_unique_id", None), media_size
    )

    async def download_under_identity_lock():
        if identity:
            existing = await dedup.find_identity(identity)
            if existing:
                node._verified_saved_ids = getattr(node, "_verified_saved_ids", set())
                node._verified_saved_ids.add(message.id)
                logger.info(
                    "Telegram media identity already saved: message_id={}", message.id
                )
                return DownloadStatus.SkipDownload, None
        return await _download_media_to_share(
            client,
            message,
            node,
            media_size,
            temp_file_name,
            file_name,
            ui_file_name,
            task_start_time,
            dedup,
            identity,
        )

    if identity:
        async with dedup.lock_for(("identity", identity)):
            return await download_under_identity_lock()
    return await download_under_identity_lock()


async def _download_media_to_share(
    client,
    message,
    node,
    media_size,
    temp_file_name,
    file_name,
    ui_file_name,
    task_start_time,
    dedup,
    identity,
):
    """Download once, then deduplicate verified bytes before the final move."""
    message_id = message.id
    for retry in range(3):
        try:
            temp_download_path = await client.download_media(
                message,
                file_name=temp_file_name,
                progress=update_download_status,
                progress_args=(
                    message_id,
                    ui_file_name,
                    task_start_time,
                    node,
                    client,
                ),
            )

            if temp_download_path and isinstance(temp_download_path, str):
                _check_download_finish(media_size, temp_download_path, ui_file_name)
                await asyncio.sleep(0.5)
                sha256 = await dedup.hash_temp(temp_download_path, app.temp_save_path)
                if sha256 is None:
                    # An unavailable hash/index must not lose a valid download.
                    async with dedup.lock_for(("target", os.path.abspath(file_name))):
                        target = file_name
                        if os.path.lexists(target):
                            target = dedup.collision_path(target, "unindexed")
                        _move_to_download_path(temp_download_path, target)
                        return DownloadStatus.SuccessDownload, target

                async with dedup.lock_for(("sha256", sha256)):
                    async with dedup.lock_for(("target", os.path.abspath(file_name))):
                        duplicate = await dedup.find_hash(sha256)
                        if duplicate is None and os.path.lexists(file_name):
                            # The target may predate this database. Verify it directly.
                            if await dedup.verified_path_digest(file_name) == sha256:
                                duplicate = file_name
                        if duplicate:
                            node._verified_saved_ids = getattr(
                                node, "_verified_saved_ids", set()
                            )
                            node._verified_saved_ids.add(message.id)
                            hashed = await dedup.hash_file(duplicate)
                            if hashed:
                                await dedup.record(
                                    duplicate, hashed[0], hashed[1], identity
                                )
                            try:
                                os.remove(temp_download_path)
                            except OSError as exc:
                                logger.warning(
                                    "duplicate temporary cleanup failed: {}",
                                    type(exc).__name__,
                                )
                            return DownloadStatus.SkipDownload, None

                        target = file_name
                        if os.path.lexists(target):
                            target = dedup.collision_path(target, sha256)
                        _move_to_download_path(temp_download_path, target)
                        hashed = await dedup.hash_file(target)
                        if hashed:
                            await dedup.record(target, hashed[0], hashed[1], identity)
                        return DownloadStatus.SuccessDownload, target
        except pyrogram.errors.exceptions.bad_request_400.BadRequest:
            logger.warning(
                f"Message[{message.id}]: {_t('file reference expired, refetching')}..."
            )
            await asyncio.sleep(RETRY_TIME_OUT)
            message = await fetch_message(client, message)
            if _check_timeout(retry, message.id):
                # pylint: disable = C0301
                logger.error(
                    f"Message[{message.id}]: "
                    f"{_t('file reference expired for 3 retries, download skipped.')}"
                )
        except pyrogram.errors.exceptions.flood_420.FloodWait as wait_err:
            await asyncio.sleep(wait_err.value)
            logger.warning("Message[{}]: FlowWait {}", message.id, wait_err.value)
            _check_timeout(retry, message.id)
        except TypeError:
            # pylint: disable = C0301
            logger.warning(
                f"{_t('Timeout Error occurred when downloading Message')}[{message.id}], "
                f"{_t('retrying after')} {RETRY_TIME_OUT} {_t('seconds')}"
            )
            await asyncio.sleep(RETRY_TIME_OUT)
            if _check_timeout(retry, message.id):
                logger.error(
                    f"Message[{message.id}]: {_t('Timing out after 3 reties, download skipped.')}"
                )
        except Exception as e:
            # pylint: disable = C0301
            logger.error(
                f"Message[{message.id}]: "
                f"{_t('could not be downloaded due to following exception')}:\n[{e}].",
                exc_info=True,
            )
            break

    return DownloadStatus.FailedDownload, None


def _load_config():
    """Load config"""
    app.load_config()


def _check_config() -> bool:
    """Check config"""
    print_meta(logger)
    try:
        _load_config()
        logger.add(
            os.path.join(app.log_file_path, "archiver.log"),
            rotation="10 MB",
            retention="10 days",
            level=app.log_level,
        )
    except Exception as e:
        logger.exception(f"load config error: {e}")
        return False

    return True


async def worker(client: pyrogram.client.Client):
    """Work for download task"""
    while app.is_running:
        item = None
        try:
            item = await queue.get()
            message = item[0]
            node: TaskNode = item[1]

            if node.is_stop_transmission:
                continue

            persistent_queue = getattr(node, "_persistent_queue", None)
            if persistent_queue is not None:
                try:
                    should_run = await persistent_queue.mark_running(
                        node.chat_id, message.id
                    )
                except Exception as exc:
                    # A transient database fault must not discard an in-memory
                    # download; the persisted row remains retryable on restart.
                    logger.warning(
                        "persistent task start unavailable: %s", type(exc).__name__
                    )
                    should_run = True
                if not should_run:
                    continue
            if node.bot:
                getattr(node, "_manual_started_ids", set()).add(message.id)
            if node.client:
                await download_task(node.client, message, node)
            else:
                await download_task(client, message, node)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"{e}")
        finally:
            if item is not None:
                queue.task_done()


async def download_chat_task(
    client: pyrogram.Client,
    chat_download_config: ChatDownloadConfig,
    node: TaskNode,
):
    """Download all task"""
    messages_iter = get_chat_history_v2(
        client,
        node.chat_id,
        limit=node.limit,
        max_id=node.end_offset_id,
        offset_id=chat_download_config.last_read_message_id,
        reverse=True,
    )

    chat_download_config.node = node

    if chat_download_config.ids_to_retry:
        logger.info(f"{_t('Downloading files failed during last run')}...")
        skipped_messages: list = await client.get_messages(  # type: ignore
            chat_id=node.chat_id, message_ids=chat_download_config.ids_to_retry
        )

        for message in skipped_messages:
            await add_download_task(message, node)

    async for message in messages_iter:  # type: ignore
        meta_data = MetaData()

        caption = message.caption
        if caption:
            caption = validate_title(caption)
            app.set_caption_name(node.chat_id, message.media_group_id, caption)
            app.set_caption_entities(
                node.chat_id, message.media_group_id, message.caption_entities
            )
        else:
            caption = app.get_caption_name(node.chat_id, message.media_group_id)
        set_meta_data(meta_data, message, caption)

        if app.need_skip_message(chat_download_config, message.id):
            continue

        if app.exec_filter(chat_download_config, meta_data):
            await add_download_task(message, node)
        else:
            node.download_status[message.id] = DownloadStatus.SkipDownload
            if message.media_group_id:
                await upload_telegram_chat(
                    client,
                    node.upload_user,
                    app,
                    node,
                    message,
                    DownloadStatus.SkipDownload,
                )

    chat_download_config.need_check = True
    chat_download_config.total_task = node.total_task
    node.is_running = True


async def download_all_chat(client: pyrogram.Client):
    """Download All chat"""
    for key, value in app.chat_download_config.items():
        value.node = TaskNode(chat_id=key)
        try:
            await download_chat_task(client, value, value.node)
        except Exception as e:
            logger.warning(f"Download {key} error: {e}")
        finally:
            value.need_check = True


async def run_until_all_task_finish():
    """Normal download"""
    while True:
        finish: bool = True
        for _, value in app.chat_download_config.items():
            if not value.need_check or value.total_task != value.finish_task:
                finish = False

        if (not app.bot_token and finish) or app.restart_program:
            break

        await asyncio.sleep(1)


def _exec_loop():
    """Exec loop"""

    app.loop.run_until_complete(run_until_all_task_finish())


async def start_server(client: pyrogram.Client):
    """
    Start the server using the provided client.
    """
    await client.start()


async def stop_server(client: pyrogram.Client):
    """
    Stop the server using the provided client.
    """
    await client.stop()


def main():
    """Main function of the downloader."""
    tasks = []
    client = HookClient(
        "media_downloader",
        api_id=app.api_id,
        api_hash=app.api_hash,
        proxy=app.proxy,
        workdir=app.session_file_path,
        start_timeout=app.start_timeout,
        no_updates=True,
    )
    try:
        app.pre_run()
        if app.web_enabled:
            init_web(app)

        set_max_concurrent_transmissions(client, app.max_concurrent_transmissions)

        app.loop.run_until_complete(start_server(client))
        logger.success(_t("Successfully started (Press Ctrl+C to stop)"))

        app.loop.create_task(download_all_chat(client))
        for _ in range(app.max_download_task):
            task = app.loop.create_task(worker(client))
            tasks.append(task)

        if app.bot_token:
            app.loop.run_until_complete(
                start_download_bot(app, client, add_download_task, download_chat_task)
            )
        _exec_loop()
    except KeyboardInterrupt:
        logger.info(_t("KeyboardInterrupt"))
    except Exception as e:
        logger.exception("{}", e)
    finally:
        app.is_running = False
        if app.bot_token:
            app.loop.run_until_complete(stop_download_bot())
        app.loop.run_until_complete(stop_server(client))
        for task in tasks:
            task.cancel()
        logger.info(_t("Stopped!"))
        logger.info(f"{_t('update config')}......")
        app.update_config()
        logger.success(
            f"{_t('Updated last read message_id to config file')},"
            f"{_t('total download')} {app.total_download_task}, "
            f"{_t('total upload file')} "
            f"{app.cloud_drive_config.total_upload_success_file_count}"
        )


if __name__ == "__main__":
    if _check_config():
        main()
