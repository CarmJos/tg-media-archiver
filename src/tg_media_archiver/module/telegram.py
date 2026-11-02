"""Telegram client behavior shared by the headless service."""

import mimetypes

from pyrogram import Client


class ArchiveClient(Client):
    async def authorize(self):
        raise RuntimeError("Telegram session is missing; run 'tg-media-archiver login'")


def file_extension(kind, mime_type=None):
    defaults = {
        "photo": ".jpg",
        "video": ".mp4",
        "document": ".bin",
    }
    return mimetypes.guess_extension(mime_type or "") or defaults.get(kind, ".bin")
