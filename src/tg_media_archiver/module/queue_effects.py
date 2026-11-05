"""Deliver durable download cleanup and completion effects outside workers."""

import asyncio
import logging
import time

from pyrogram import raw
from pyrogram.errors import BadRequest

logger = logging.getLogger(__name__)


class QueueEffects:
    def __init__(self, queue, client, owner_id, interval=2):
        self.queue = queue
        self.client = client
        self.owner_id = int(owner_id)
        self.interval = interval
        self.delete_retry_at = {}
        self.next_request_at = 0.0
        self.next_notice_at = 0.0

    async def tick(self):
        if time.monotonic() < self.next_request_at:
            return
        if time.monotonic() >= self.next_notice_at:
            await self._notify_completion()
        if time.monotonic() < self.next_request_at:
            return
        await self._delete_saved_forwards()

    async def _delete_saved_forwards(self):
        rows = await self.queue.list_cleanup(self.owner_id, limit=1000)
        message_ids = []
        for row in rows:
            chat_id, message_id = int(row["chat_id"]), int(row["message_id"])
            if chat_id != self.owner_id or message_id <= 0:
                logger.error(
                    "cleanup rejected a message outside the owner private chat"
                )
                continue
            if time.monotonic() < self.delete_retry_at.get(message_id, 0):
                continue
            message_ids.append(message_id)
            if len(message_ids) == 100:
                break
        if not message_ids:
            return
        await self._delete_group(message_ids, budget=20)

    def _defer_delete(self, message_ids, delay):
        retry_at = time.monotonic() + delay
        for message_id in message_ids:
            self.delete_retry_at[message_id] = retry_at

    async def _delete_group(self, message_ids, budget):
        if not message_ids:
            return budget
        if budget <= 0:
            self._defer_delete(message_ids, 30)
            return 0
        budget -= 1
        try:
            await self.client.delete_messages(self.owner_id, message_ids, revoke=True)
        except Exception as exc:
            kind = type(exc).__name__
            logger.warning(
                "saved media message delete deferred: count=%s type=%s",
                len(message_ids),
                kind,
            )
            if kind == "FloodWait":
                delay = max(1, float(getattr(exc, "value", 30)))
                self.next_request_at = time.monotonic() + delay
                self._defer_delete(message_ids, delay)
                return budget
            if isinstance(exc, BadRequest) and len(message_ids) > 1:
                midpoint = len(message_ids) // 2
                budget = await self._delete_group(message_ids[:midpoint], budget)
                if time.monotonic() >= self.next_request_at:
                    return await self._delete_group(message_ids[midpoint:], budget)
                self._defer_delete(
                    message_ids[midpoint:], self.next_request_at - time.monotonic()
                )
                return budget
            self._defer_delete(message_ids, 300)
            return budget
        for message_id in message_ids:
            await self.queue.ack_cleanup(self.owner_id, message_id)
            self.delete_retry_at.pop(message_id, None)
        logger.info("saved media messages deleted: count=%s", len(message_ids))
        return budget

    async def _notify_completion(self):
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
                    message=notice["summary_text"],
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
            self.next_notice_at = time.monotonic() + delay
            if kind == "FloodWait":
                self.next_request_at = self.next_notice_at

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception as exc:
                logger.warning("queue effects retry pending: %s", type(exc).__name__)
            await asyncio.sleep(self.interval)
