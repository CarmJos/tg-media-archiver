"""Archive queued private forwards with one Telegram bot connection."""

import asyncio
import logging
import os
import sqlite3
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pyrogram import idle
from pyrogram.errors import BadRequest, FloodWait

from tg_media_archiver.module.app import Application, DownloadStatus
from tg_media_archiver.module.bot import DownloadBot
from tg_media_archiver.module.download_stat import (
    get_download_result,
    update_download_status,
)
from tg_media_archiver.module.media_dedup import MediaDedup
from tg_media_archiver.module.share_files import publish_download
from tg_media_archiver.module.telegram import file_extension
from tg_media_archiver.utils.format import truncate_filename, validate_title

logger = logging.getLogger(__name__)


class Archiver:
    def __init__(self, app):
        self.app = app
        self.queue = asyncio.Queue()
        self.dedup = MediaDedup(app.save_path)
        self.bot = DownloadBot(app, self.add_download_task)

    async def add_download_task(self, message, node):
        node.download_status[message.id] = DownloadStatus.Downloading
        await self.queue.put((message, node))

    def media_paths(self, message, kind, media):
        original = getattr(media, "file_name", None)
        original = (
            os.path.basename(str(original).replace("\\", "/")) if original else ""
        )
        stem, suffix = os.path.splitext(original)
        if not suffix:
            suffix = file_extension(kind, getattr(media, "mime_type", None))
        if not stem and kind == "photo":
            stem = getattr(media, "file_unique_id", "")
        name = self.app.get_file_name(
            message.id, stem, getattr(message, "caption", None)
        )
        name = truncate_filename(validate_title(name + suffix))
        title = getattr(message.chat, "title", None) or str(message.chat.id)
        date = message.date.strftime(self.app.date_format) if message.date else "0"
        directory = self.app.get_file_save_path(kind, validate_title(title), date)
        target = os.path.join(directory, name)
        temporary = os.path.join(self.app.temp_save_path, str(message.chat.id), name)
        return target, temporary

    async def download_media(self, client, message, node):
        for kind in self.app.media_types:
            media = getattr(message, kind, None)
            if media is not None:
                break
        else:
            raise ValueError("Message has no supported media")

        target, temporary = self.media_paths(message, kind, media)
        allowed = self.app.file_formats.get(kind, ["all"])
        mime = (getattr(media, "mime_type", None) or "").split("/")[-1]
        extension = Path(target).suffix.lstrip(".").lower()
        if "all" not in allowed and mime not in allowed and extension not in allowed:
            raise ValueError("Media format is excluded by configuration")
        size = int(getattr(media, "file_size", 0) or 0)
        identity = self.dedup.identity(
            kind, getattr(media, "file_unique_id", None), size
        )
        lock_key = (
            ("identity", identity)
            if identity
            else ("message", node.chat_id, message.id)
        )
        async with self.dedup.lock_for(lock_key):
            existing = await self.dedup.find_identity(identity)
            if existing:
                return DownloadStatus.SkipDownload, existing
            Path(temporary).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            started = time.time()
            for attempt in range(3):
                try:
                    downloaded = await client.download_media(
                        message,
                        file_name=temporary,
                        progress=update_download_status,
                        progress_args=(message.id, target, started, node),
                    )
                    if not downloaded or not os.path.isfile(downloaded):
                        raise OSError("Download did not produce a file")
                    actual_size = os.path.getsize(downloaded)
                    if actual_size <= 0 or (size and actual_size != size):
                        raise OSError("Downloaded file size does not match Telegram")
                    return await self.publish(downloaded, target, identity)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "Download %s attempt %s failed: %s",
                        message.id,
                        attempt + 1,
                        type(exc).__name__,
                    )
                    if attempt == 2:
                        raise
                    await asyncio.sleep(
                        max(1, exc.value) if isinstance(exc, FloodWait) else 3
                    )
                    if isinstance(exc, BadRequest):
                        refreshed = await client.get_messages(
                            node.chat_id, message.id, replies=0
                        )
                        refreshed_media = getattr(refreshed, kind, None)
                        refreshed_identity = self.dedup.identity(
                            kind,
                            getattr(refreshed_media, "file_unique_id", None),
                            getattr(refreshed_media, "file_size", 0),
                        )
                        if refreshed_media is None or refreshed_identity != identity:
                            raise ValueError(
                                "Forwarded media changed during download"
                            ) from exc
                        message = refreshed

    async def publish(self, temporary, target, identity):
        digest = await self.dedup.hash_temp(temporary, self.app.temp_save_path)
        async with self.dedup.lock_for(("sha256", digest or target)):
            async with self.dedup.lock_for(("target", os.path.abspath(target))):
                duplicate = await self.dedup.find_hash(digest) if digest else None
                if digest and duplicate is None and os.path.lexists(target):
                    if await self.dedup.verified_path_digest(target) == digest:
                        duplicate = target
                if duplicate:
                    await self.dedup.register_existing(duplicate, identity)
                    Path(temporary).unlink(missing_ok=True)
                    return DownloadStatus.SkipDownload, duplicate
                if os.path.lexists(target):
                    target = self.dedup.collision_path(target, digest or "unindexed")
                await asyncio.to_thread(
                    publish_download, temporary, target, self.app.save_path
                )
                await self.dedup.register_existing(target, identity)
                return DownloadStatus.SuccessDownload, target

    async def download_task(self, client, message, node):
        status = DownloadStatus.FailedDownload
        error = "DownloadFailed"
        interrupted = False
        try:
            status, saved_path = await self.download_media(client, message, node)
            if not saved_path or not os.path.isfile(saved_path):
                raise OSError("Archived file is no longer present")
            logger.info("Archived message %s: %s", message.id, status.name)
        except asyncio.CancelledError:
            interrupted = True
            raise
        except Exception as exc:
            status, error = DownloadStatus.FailedDownload, type(exc).__name__
            logger.error("Could not archive message %s: %s", message.id, error)
        finally:
            if interrupted:
                status = DownloadStatus.Downloading
                node._manual_started_ids.discard(message.id)
                chat_stats = get_download_result().get(node.chat_id)
                if chat_stats is not None:
                    chat_stats.pop(message.id, None)
                    if not chat_stats:
                        get_download_result().pop(node.chat_id, None)
            node.download_status[message.id] = status
            if node._persistent_queue is not None:
                try:
                    if interrupted:
                        await node._persistent_queue.mark_queued(
                            node.chat_id, message.id
                        )
                    elif status in (
                        DownloadStatus.SuccessDownload,
                        DownloadStatus.SkipDownload,
                    ):
                        outcome = (
                            "downloaded"
                            if status is DownloadStatus.SuccessDownload
                            else "skipped"
                        )
                        await node._persistent_queue.mark_done(
                            node.chat_id, message.id, outcome
                        )
                    else:
                        await node._persistent_queue.mark_failed(
                            node.chat_id, message.id, error
                        )
                except Exception as exc:
                    logger.error(
                        "Queue settlement deferred until restart: %s",
                        type(exc).__name__,
                    )
            if self.bot.manual_progress is not None:
                self.bot.manual_progress.snapshot()
            self.bot._scheduled_ids.discard(message.id)

    async def worker(self):
        while True:
            message, node = await self.queue.get()
            try:
                delay = 1
                while True:
                    try:
                        claimed = await node._persistent_queue.mark_running(
                            node.chat_id, message.id
                        )
                        break
                    except (OSError, sqlite3.Error) as exc:
                        logger.warning("Queue claim deferred: %s", type(exc).__name__)
                        await asyncio.sleep(delay)
                        delay = min(30, delay * 2)
                if not claimed:
                    node.download_status[message.id] = DownloadStatus.SkipDownload
                    self.bot.manual_progress.snapshot()
                    self.bot._scheduled_ids.discard(message.id)
                    continue
                node._manual_started_ids.add(message.id)
                await self.download_task(node.client, message, node)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                node.download_status[message.id] = DownloadStatus.FailedDownload
                self.bot.manual_progress.snapshot()
                self.bot._scheduled_ids.discard(message.id)
                logger.error("Queued task retained for restart: %s", type(exc).__name__)
            finally:
                self.queue.task_done()

    async def run(self):
        workers = []
        try:
            await self.bot.start()
            index = await self.dedup.refresh_index()
            logger.info(
                "Archive index refreshed: indexed=%s unchanged=%s removed=%s errors=%s",
                index["indexed"],
                index["unchanged"],
                index["removed"],
                index["errors"],
            )
            workers = [
                asyncio.create_task(self.worker())
                for _ in range(self.app.max_download_task)
            ]
            logger.info("Archiver started; listening for private forwarded media")
            await idle()
        finally:
            await self.bot.stop_accepting()
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            try:
                await self.bot.stop()
            except Exception as exc:
                logger.error("Telegram shutdown failed: %s", type(exc).__name__)
            logger.info("Archiver stopped; unfinished tasks remain in files.db")


def main():
    app = Application()
    try:
        app.load_config()
        app.pre_run()
        logging.basicConfig(
            level=app.log_level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=[
                logging.StreamHandler(),
                RotatingFileHandler(
                    os.path.join(app.log_file_path, "archiver.log"),
                    maxBytes=10 * 1024 * 1024,
                    backupCount=3,
                    encoding="utf-8",
                ),
            ],
        )
        logging.getLogger("pyrogram").setLevel(logging.WARNING)
        asyncio.run(Archiver(app).run())
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logger.error("Archiver stopped: %s", type(exc).__name__)
        return 1
