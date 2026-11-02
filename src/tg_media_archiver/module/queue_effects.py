"""Deliver durable download cleanup and completion effects outside workers."""

import asyncio
import logging
import time

from pyrogram import raw

logger = logging.getLogger(__name__)
COMPLETION_TEXT = "队列内所有视频均已完成下载。"


class QueueEffects:
    def __init__(self, queue, client, owner_id, interval=2):
        self.queue = queue
        self.client = client
        self.owner_id = int(owner_id)
        self.interval = interval
        self.delete_retry_at = {}
        self.next_request_at = 0.0

    async def tick(self):
        if time.monotonic() < self.next_request_at:
            return
        rows = await self.queue.list_cleanup(self.owner_id, limit=1000)
        for row in rows:
            chat_id, message_id = int(row["chat_id"]), int(row["message_id"])
            if chat_id != self.owner_id or message_id <= 0:
                logger.error(
                    "cleanup rejected a message outside the owner private chat"
                )
                continue
            if time.monotonic() < self.delete_retry_at.get(message_id, 0):
                continue
            try:
                await self.client.delete_messages(chat_id, message_id, revoke=True)
                await self.queue.ack_cleanup(chat_id, message_id)
                self.delete_retry_at.pop(message_id, None)
                logger.info("saved media message deleted: id=%s", message_id)
            except Exception as exc:
                kind = type(exc).__name__
                logger.warning(
                    "saved media message delete deferred: id=%s type=%s",
                    message_id,
                    kind,
                )
                if kind == "FloodWait":
                    self.next_request_at = time.monotonic() + max(
                        1, float(getattr(exc, "value", 30))
                    )
                    return
                self.delete_retry_at[message_id] = time.monotonic() + 300

        notice = await self.queue.next_completion(self.owner_id)
        if notice is None:
            return
        if int(notice["owner_id"]) != self.owner_id:
            logger.error("completion rejected an unexpected owner")
            return
        try:
            # Persisted random_id lets Telegram deduplicate a retry after a crash
            # between successful send and the local acknowledgement.
            await self.client.invoke(
                raw.functions.messages.SendMessage(
                    peer=await self.client.resolve_peer(self.owner_id),
                    message=COMPLETION_TEXT,
                    random_id=int(notice["notification_random_id"]),
                    no_webpage=True,
                )
            )
            await self.queue.ack_completion(notice["id"])
            logger.info("queue completion notice delivered: batch=%s", notice["id"])
        except Exception as exc:
            kind = type(exc).__name__
            logger.warning("queue completion notice deferred: %s", kind)
            delay = (
                max(1, float(getattr(exc, "value", 30))) if kind == "FloodWait" else 30
            )
            self.next_request_at = time.monotonic() + delay

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception as exc:
                logger.warning("queue effects retry pending: %s", type(exc).__name__)
            await asyncio.sleep(self.interval)
