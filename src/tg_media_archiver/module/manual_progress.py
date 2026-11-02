"""On-demand download progress in the owner's private chat."""

import asyncio
import html
import json
import logging
import os
import time
from collections import deque

from pyrogram.enums import ParseMode

from tg_media_archiver.module.download_stat import get_download_result

logger = logging.getLogger(__name__)


def _bytes(value):
    value = max(0, float(value or 0))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024


def _error_kind(exc):
    name = type(exc).__name__
    if name == "FloodWait":
        return "flood"
    if name == "MessageNotModified":
        return "same"
    if name in (
        "MessageIdInvalid",
        "MessageDeleteForbidden",
        "MessageEmpty",
        "PeerIdInvalid",
        "ChatWriteForbidden",
    ):
        return "gone"
    return "other"


class ManualProgress:
    """Single editable panel; counters are bounded by current work plus recent items."""

    def __init__(
        self,
        owner_id,
        refresh_seconds=3,
        state_path=None,
        reposition_debounce=1.0,
        reposition_max_delay=3.0,
    ):
        self.owner_id = int(owner_id)
        self.refresh_seconds = refresh_seconds
        self.state_path = os.fspath(state_path) if state_path is not None else None
        self._state_ready = state_path is None
        self.reposition_debounce = reposition_debounce
        self.reposition_max_delay = reposition_max_delay
        self.pending = {}
        self.recent = deque(maxlen=256)
        self.counts = {"success": 0, "failed": 0, "skipped": 0}
        self.unclassified = 0
        self.total = 0
        self.last_arrival = 0.0
        self.panel_id = None
        self.generation = 0
        self.enabled = False
        self.loop_task = None
        self.next_edit_after = 0.0
        self._last_text = None
        self.opening = False
        self.activity_revision = 0
        self.preparing_count = 0
        self.delete_retry_seconds = 0.5
        self._delete_tasks = set()
        self._move_task = None
        self._latest_incoming_id = 0
        self._last_incoming_at = 0.0
        self._first_incoming_at = 0.0
        self.next_move_after = 0.0

    def _save_state(self):
        if self.state_path is None or not self._state_ready:
            return
        data = {
            "owner_id": self.owner_id,
            "panel_id": self.panel_id,
            "generation": self.generation,
            "enabled": self.enabled,
            "total": self.total,
            "counts": self.counts,
            "unclassified": self.unclassified,
            "pending_message_ids": [value[1] for value in self.pending.values()],
            "latest_incoming_id": self._latest_incoming_id,
        }
        temporary = self.state_path + ".tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                json.dump(data, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
        except OSError as exc:
            logger.warning("progress panel state unavailable: %s", type(exc).__name__)

    async def restore(self, client, markup, button, bot_id):
        """Resume a persisted panel only after checking Bot ownership and callback."""
        if self.state_path is None:
            return False
        self._state_ready = True
        if not os.path.isfile(self.state_path):
            return False
        try:
            with open(self.state_path, encoding="utf-8") as stream:
                data = json.load(stream)
            owner_id = int(data["owner_id"])
            panel_id = int(data["panel_id"])
            generation = int(data["generation"])
            if owner_id != self.owner_id or panel_id <= 0 or generation < 0:
                return False
            for attempt in range(3):
                try:
                    message = await client.get_messages(
                        self.owner_id, panel_id, replies=0
                    )
                    break
                except Exception as exc:
                    if attempt == 2:
                        raise
                    if _error_kind(exc) == "flood":
                        delay = max(0.0, float(getattr(exc, "value", 30)))
                        if delay > 30:
                            raise
                    else:
                        delay = attempt + 1
                    await asyncio.sleep(delay)
            if not message or getattr(message, "empty", False):
                return False
            chat = getattr(message, "chat", None)
            chat_type = getattr(chat, "type", None)
            if (
                getattr(chat, "id", None) != self.owner_id
                or getattr(chat_type, "value", chat_type) != "private"
                or getattr(getattr(message, "from_user", None), "id", None)
                != int(bot_id)
            ):
                return False
            message_text = str(getattr(message, "text", "") or "")
            if not (
                message_text.startswith(("📥 下载进度", "✅ 下载完成"))
                or ("处理进度 [" in message_text and "总计 " in message_text)
            ):
                return False
            keyboard = (
                getattr(getattr(message, "reply_markup", None), "inline_keyboard", [])
                or []
            )
            if not any(
                getattr(key, "callback_data", None) == f"mp:off:{generation}"
                for row in keyboard
                for key in row
            ):
                return False
            counts = data["counts"]
            restored = {name: max(0, int(counts[name])) for name in self.counts}
            unclassified = max(0, int(data.get("unclassified", 0)))
            total = max(0, int(data["total"]))
            active = len(self.pending)
            total = max(total, sum(restored.values()) + unclassified + active)
            unclassified += total - (sum(restored.values()) + unclassified + active)
            self.counts = restored
            self.unclassified = unclassified
            self.total = total
            self.panel_id = panel_id
            self.generation = generation
            self.enabled = bool(data["enabled"]) or bool(self.pending)
            self._latest_incoming_id = max(
                int(data.get("latest_incoming_id", 0)), panel_id
            )
            self._last_text = None
            self.last_arrival = time.monotonic()
            self.next_edit_after = 0.0
            self._save_state()
            if self.enabled:
                self.loop_task = asyncio.create_task(
                    self._loop(client, markup, button, self.generation)
                )
            if self._latest_incoming_id > self.panel_id:
                now = time.monotonic()
                self._first_incoming_at = now
                self._last_incoming_at = now
                self._move_task = asyncio.create_task(
                    self._move_loop(client, markup, button)
                )
            return True
        except Exception as exc:
            logger.warning("progress panel restore unavailable: %s", type(exc).__name__)
            return False

    def track(self, node, message):
        if not self.pending and self.total and not self.enabled:
            self.recent.clear()
            self.counts = {"success": 0, "failed": 0, "skipped": 0}
            self.total = 0
            self.unclassified = 0
        key = node.task_id
        if key in self.pending:
            return
        media = getattr(message, "media", None)
        item = getattr(message, getattr(media, "value", ""), None)
        name = (
            getattr(item, "file_name", None)
            or f"{getattr(media, 'value', 'media')}_{message.id}"
        )
        self.pending[key] = (node, message.id, str(name))
        self.total += 1
        self.last_arrival = time.monotonic()
        self.activity_revision += 1
        self._save_state()

    def snapshot(self):
        active = []
        queued = 0
        self.preparing_count = 0
        download_result = get_download_result()
        for task_id, (node, message_id, name) in list(self.pending.items()):
            status = node.download_status.get(message_id)
            status_name = getattr(status, "name", "")
            if status_name in ("SuccessDownload", "FailedDownload", "SkipDownload"):
                chat_stats = download_result.get(node.chat_id)
                if chat_stats is not None:
                    chat_stats.pop(message_id, None)
                    if not chat_stats:
                        download_result.pop(node.chat_id, None)
                label = {
                    "SuccessDownload": "success",
                    "FailedDownload": "failed",
                    "SkipDownload": "skipped",
                }[status_name]
                self.counts[label] += 1
                self.recent.append((name, label))
                del self.pending[task_id]
                continue
            started = message_id in getattr(node, "_manual_started_ids", set())
            if not started:
                queued += 1
                continue
            stat = download_result.get(node.chat_id, {}).get(message_id, {})
            if stat.get("task_id") != task_id:
                stat = {}
            down_byte = int(stat.get("down_byte", 0) or 0)
            total_size = int(stat.get("total_size", 0) or 0)
            if down_byte <= 0 or (total_size > 0 and down_byte >= total_size):
                self.preparing_count += 1
                continue
            speed = stat.get("download_speed", 0)
            if time.time() - stat.get("end_time", 0) > 5:
                speed = 0
            active.append((name, int(speed or 0), down_byte, total_size))
        finished = bool(
            self.total
            and not self.pending
            and time.monotonic() - self.last_arrival >= self.refresh_seconds
        )
        return queued, active, finished

    def render(self, final=False):
        queued, active, finished = self.snapshot()
        done = sum(self.counts.values()) + self.unclassified
        percent = (done * 100 // self.total) if self.total else 0
        filled = min(10, (done * 10 // self.total) if self.total else 0)
        bar = "█" * filled + "░" * (10 - filled)
        title = "✅ 下载完成" if final or finished else "📥 下载进度"
        lines = [
            f"<b>{title}</b>",
            f"总计 {self.total} · 排队 {queued} · 准备或等待 {self.preparing_count} · 下载中 {len(active)}",
            f"成功 {self.counts['success']} · 失败 {self.counts['failed']} · 跳过 {self.counts['skipped']}",
            f"处理进度 [{bar}] {done}/{self.total} ({percent}%)",
        ]
        if active:
            lines.append(f"总速度 {_bytes(sum(row[1] for row in active))}/s")
            for name, speed, used, size in active[:5]:
                plain_name = " ".join(os.path.basename(name).split())[:80]
                safe_name = html.escape(plain_name, quote=False)
                amount = f"{_bytes(used)}/{_bytes(size)}" if size else _bytes(used)
                lines.append(f"• {safe_name} — {amount}, {_bytes(speed)}/s")
            if len(active) > 5:
                lines.append(f"另有 {len(active) - 5} 个文件正在下载")
        elif queued or self.preparing_count:
            lines.append("文件正在排队、准备或等待传输")
        if self.unclassified:
            lines.append(f"已处理（状态未分类）{self.unclassified}")
        self._save_state()
        return "\n".join(lines), finished

    def _keyboard(self, markup, button, generation=None):
        generation = self.generation if generation is None else generation
        return markup([[button("关闭进度", callback_data=f"mp:off:{generation}")]])

    async def _safe_edit(self, client, text, keyboard=None):
        panel_id = self.panel_id
        generation = self.generation
        if panel_id is None:
            return False
        try:
            await client.edit_message_text(
                self.owner_id,
                panel_id,
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            if generation == self.generation and panel_id == self.panel_id:
                self._last_text = text
                self._save_state()
            return True
        except Exception as exc:
            logger.warning("progress panel edit failed: %s", type(exc).__name__)
            kind = _error_kind(exc)
            if generation != self.generation or panel_id != self.panel_id:
                return False
            if kind == "flood":
                self.next_edit_after = time.monotonic() + max(
                    0, float(getattr(exc, "value", 3))
                )
            elif kind == "gone":
                self.enabled = False
                self.panel_id = None
                self._save_state()
            return kind == "same"

    async def _safe_delete(self, client, panel_id):
        try:
            await client.delete_messages(self.owner_id, panel_id, revoke=True)
        except Exception as exc:
            logger.warning("progress panel delete failed: %s", type(exc).__name__)
            if _error_kind(exc) == "gone":
                return
            task = asyncio.create_task(self._retry_delete(client, panel_id, exc))
            self._delete_tasks.add(task)
            task.add_done_callback(self._delete_tasks.discard)

    async def _retry_delete(self, client, panel_id, previous_error):
        for attempt in (2, 3):
            if _error_kind(previous_error) == "flood":
                delay = max(0, float(getattr(previous_error, "value", 3)))
            else:
                delay = self.delete_retry_seconds * (attempt - 1)
            await asyncio.sleep(delay)
            try:
                await client.delete_messages(self.owner_id, panel_id, revoke=True)
                return
            except Exception as exc:
                if _error_kind(exc) == "gone":
                    return
                previous_error = exc
        logger.error(
            "progress panel delete exhausted retries: %s", type(previous_error).__name__
        )

    async def open(self, client, markup, button):
        if self.enabled and self.opening:
            return
        if self.panel_id is not None:
            was_enabled = self.enabled
            if not was_enabled:
                self.generation += 1
                self.enabled = True
                self.next_edit_after = 0.0
            reuse_generation = self.generation
            text, finished = self.render()
            edited = False
            if time.monotonic() >= self.next_edit_after:
                edited = await self._safe_edit(
                    client, text, self._keyboard(markup, button)
                )
            if self.generation != reuse_generation:
                return
            if self.panel_id is not None:
                if not was_enabled:
                    if finished and edited:
                        self.enabled = False
                    else:
                        self.loop_task = asyncio.create_task(
                            self._loop(client, markup, button, self.generation)
                        )
                return
        self.generation += 1
        token = self.generation
        self.enabled = True
        self.panel_id = None
        self.opening = True
        self.next_edit_after = 0.0
        text, _ = self.render()
        try:
            sent = await client.send_message(
                self.owner_id,
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=self._keyboard(markup, button),
                disable_notification=True,
            )
        except Exception as exc:
            logger.warning("progress panel send failed: %s", type(exc).__name__)
            if self.generation == token:
                self.opening = False
                self.enabled = False
            if self.generation == token and _error_kind(exc) != "flood":
                try:
                    await client.send_message(
                        self.owner_id,
                        "进度面板暂时无法打开，请稍后重试。",
                        parse_mode=ParseMode.DISABLED,
                    )
                except Exception as notice_exc:
                    logger.warning(
                        "progress panel failure notice failed: %s",
                        type(notice_exc).__name__,
                    )
            return
        if token != self.generation:
            await self._safe_delete(client, sent.id)
            return
        self.opening = False
        self.panel_id = sent.id
        self._last_text = text
        self._save_state()
        if not self.enabled:
            panel_id = self.panel_id
            self.panel_id = None
            await self._safe_delete(client, panel_id)
            return
        self.loop_task = asyncio.create_task(self._loop(client, markup, button, token))
        if self._latest_incoming_id > sent.id and self._move_task is None:
            self._move_task = asyncio.create_task(
                self._move_loop(client, markup, button)
            )

    async def close(self, client):
        self.enabled = False
        self.generation += 1
        self.opening = False
        panel_id = self.panel_id
        self.panel_id = None
        self._last_text = None
        self._save_state()
        if panel_id is not None:
            await self._safe_delete(client, panel_id)

    async def shutdown(self):
        """Stop local panel tasks while retaining the visible panel for restart."""
        tasks = [self.loop_task, self._move_task, *self._delete_tasks]
        tasks = [task for task in tasks if task is not None and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.loop_task = None
        self._move_task = None
        self._delete_tasks.clear()
        self._save_state()

    async def observe_message(self, client, message, markup, button):
        """Move an already-visible panel below new owner content after a burst."""
        chat = getattr(message, "chat", None)
        chat_type = getattr(chat, "type", None)
        message_id = getattr(message, "id", None)
        if (
            getattr(getattr(message, "from_user", None), "id", None) != self.owner_id
            or getattr(chat, "id", None) != self.owner_id
            or getattr(chat_type, "value", chat_type) != "private"
            or not isinstance(message_id, int)
            or isinstance(message_id, bool)
            or message_id <= 0
        ):
            return False
        # An unopened or explicitly closed panel has no ID. During the initial
        # send, remember arrivals so a late result cannot leave the panel above.
        if self.panel_id is None and not self.opening:
            return False
        if message_id <= max(self.panel_id or 0, self._latest_incoming_id):
            return False
        now = time.monotonic()
        if self._move_task is None or self._move_task.done():
            self._first_incoming_at = now
        self._last_incoming_at = now
        self._latest_incoming_id = message_id
        if self.panel_id is not None and not self.enabled:
            # A completed panel is still visible until the user closes it.
            self.enabled = True
            self.last_arrival = now
            self.loop_task = asyncio.create_task(
                self._loop(client, markup, button, self.generation)
            )
        self.activity_revision += 1
        self._save_state()
        if self.panel_id is not None and self._move_task is None:
            self._move_task = asyncio.create_task(
                self._move_loop(client, markup, button)
            )
        return True

    async def _move_loop(self, client, markup, button):
        delay_on_error = 1.0
        try:
            while (
                self.panel_id is not None and self._latest_incoming_id > self.panel_id
            ):
                now = time.monotonic()
                target = min(
                    self._last_incoming_at + self.reposition_debounce,
                    self._first_incoming_at + self.reposition_max_delay,
                )
                wait = max(0.0, target - now, self.next_move_after - now)
                if wait:
                    await asyncio.sleep(wait)
                    continue
                old_id = self.panel_id
                old_generation = self.generation
                text, finished = self.render()
                activity = self.activity_revision
                try:
                    sent = await client.send_message(
                        self.owner_id,
                        text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=self._keyboard(markup, button, old_generation + 1),
                        disable_notification=True,
                    )
                except Exception as exc:
                    logger.warning(
                        "progress panel move deferred: %s", type(exc).__name__
                    )
                    if _error_kind(exc) == "flood":
                        delay = max(0.0, float(getattr(exc, "value", 3)))
                    else:
                        delay = delay_on_error
                        delay_on_error = min(60.0, delay_on_error * 2)
                    self.next_move_after = time.monotonic() + delay
                    continue
                if old_id != self.panel_id or old_generation != self.generation:
                    await self._safe_delete(client, sent.id)
                    continue
                self.generation = old_generation + 1
                self.panel_id = sent.id
                self._last_text = text
                self.next_edit_after = 0.0
                self.next_move_after = 0.0
                self._save_state()
                if activity != self.activity_revision or self.pending or not finished:
                    self.enabled = True
                    self.loop_task = asyncio.create_task(
                        self._loop(client, markup, button, self.generation)
                    )
                else:
                    self.enabled = False
                    self._save_state()
                if activity != self.activity_revision:
                    current_text, _ = self.render()
                    await self._safe_edit(
                        client, current_text, self._keyboard(markup, button)
                    )
                await self._safe_delete(client, old_id)
                delay_on_error = 1.0
                if self._latest_incoming_id > sent.id:
                    self._first_incoming_at = time.monotonic()
        finally:
            self._move_task = None

    async def _loop(self, client, markup, button, generation):
        while self.enabled and self.generation == generation:
            await asyncio.sleep(self.refresh_seconds)
            if not self.enabled or self.generation != generation or not self.panel_id:
                break
            text, finished = self.render()
            if time.monotonic() < self.next_edit_after:
                continue
            activity = self.activity_revision
            edited = True
            if text != self._last_text or finished:
                edited = await self._safe_edit(
                    client, text, self._keyboard(markup, button)
                )
            if generation != self.generation:
                break
            if finished and edited and activity == self.activity_revision:
                self.enabled = False
                self._save_state()
                break

    async def callback(self, client, query):
        chat = getattr(getattr(query, "message", None), "chat", None)
        valid = (
            self.panel_id is not None
            and getattr(getattr(query, "from_user", None), "id", None) == self.owner_id
            and getattr(chat, "id", None) == self.owner_id
            and getattr(
                getattr(chat, "type", None), "value", getattr(chat, "type", None)
            )
            == "private"
            and getattr(query.message, "id", None) == self.panel_id
            and getattr(query, "data", None) == f"mp:off:{self.generation}"
        )
        if valid:
            await self.close(client)
        try:
            if valid:
                await query.answer()
            else:
                await query.answer("此按钮已失效")
        except Exception:
            pass
        return bool(valid)
