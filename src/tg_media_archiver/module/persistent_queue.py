"""Durable identifiers for Telegram downloads awaiting a worker.

The queue shares ``files.db`` with the media deduplication index and owns
``pending_downloads``, ``download_batches``, and ``download_cleanup``.
"""

import asyncio
import os
import re
import secrets
import sqlite3
import threading
import time

from tg_media_archiver.module.messages import Messages

_ERROR_TYPE = re.compile(r"^[A-Za-z_][A-Za-z_0-9]{0,127}$")
_BATCH_WINDOW_SECONDS = 3
_MAX_BATCH_SPAN_SECONDS = 30
_TERMINAL_RECOVERY_ERRORS = frozenset(
    {"MissingMessage", "InvalidForward", "UnsupportedMedia", "UnauthorizedRecord"}
)


class PersistentQueue:
    """SQLite queue keyed by the Bot chat ID and message ID."""

    def __init__(self, db_path, messages=None):
        self.db_path = os.path.abspath(os.fspath(db_path))
        self.messages = messages if messages is not None else Messages()
        self._schema_lock = threading.Lock()
        self._ready = False

    @staticmethod
    def _ids(chat_id, message_id):
        if isinstance(chat_id, bool) or isinstance(message_id, bool):
            raise ValueError("chat_id and message_id must be integers")
        chat_id, message_id = int(chat_id), int(message_id)
        if chat_id == 0 or message_id <= 0:
            raise ValueError("chat_id must be nonzero and message_id positive")
        return chat_id, message_id

    @staticmethod
    def _user_id(user_id):
        if isinstance(user_id, bool):
            raise ValueError("user_id must be a positive integer")
        user_id = int(user_id)
        if user_id <= 0:
            raise ValueError("user_id must be a positive integer")
        return user_id

    @staticmethod
    def _error_name(error_type):
        if isinstance(error_type, type) and issubclass(error_type, BaseException):
            error_type = error_type.__name__
        elif isinstance(error_type, BaseException):
            error_type = type(error_type).__name__
        elif error_type is None:
            error_type = "UnknownError"
        if not isinstance(error_type, str) or not _ERROR_TYPE.fullmatch(error_type):
            raise ValueError("error_type must be an exception class name")
        return error_type

    def _connect(self):
        parent = os.path.dirname(self.db_path)
        if not os.path.isdir(parent):
            raise FileNotFoundError(
                f"queue database directory does not exist: {parent}"
            )
        connection = sqlite3.connect(self.db_path, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA journal_mode=WAL")
            if not self._ready:
                with self._schema_lock:
                    if not self._ready:
                        with connection:
                            connection.execute(
                                """CREATE TABLE IF NOT EXISTS pending_downloads (
                                    chat_id INTEGER NOT NULL,
                                    message_id INTEGER NOT NULL,
                                    user_id INTEGER NOT NULL,
                                    state TEXT NOT NULL CHECK(state IN ('queued','running','failed','done')),
                                    attempts INTEGER NOT NULL DEFAULT 0,
                                    last_error_type TEXT,
                                    updated_at REAL NOT NULL,
                                    PRIMARY KEY(chat_id, message_id)
                                )"""
                            )
                            connection.execute(
                                "CREATE INDEX IF NOT EXISTS pending_downloads_state "
                                "ON pending_downloads(state, updated_at)"
                            )
                            columns = {
                                row[1]
                                for row in connection.execute(
                                    "PRAGMA table_info(pending_downloads)"
                                )
                            }
                            if "batch_id" not in columns:
                                connection.execute(
                                    "ALTER TABLE pending_downloads ADD COLUMN batch_id INTEGER"
                                )
                            if "outcome" not in columns:
                                connection.execute(
                                    "ALTER TABLE pending_downloads ADD COLUMN outcome TEXT"
                                )
                            connection.execute(
                                """CREATE TABLE IF NOT EXISTS download_batches (
                                    id INTEGER PRIMARY KEY,
                                    owner_id INTEGER NOT NULL,
                                    state TEXT NOT NULL CHECK(state IN ('active','ready','claimed','notified')),
                                    updated_at REAL NOT NULL,
                                    notification_random_id INTEGER NOT NULL,
                                    notified_at REAL
                                )"""
                            )
                            connection.execute(
                                """CREATE TABLE IF NOT EXISTS download_cleanup (
                                    chat_id INTEGER NOT NULL,
                                    message_id INTEGER NOT NULL,
                                    batch_id INTEGER NOT NULL,
                                    done_at REAL NOT NULL,
                                    acked_at REAL,
                                    PRIMARY KEY(chat_id,message_id)
                                )"""
                            )
                            batch_columns = {
                                row[1]
                                for row in connection.execute(
                                    "PRAGMA table_info(download_batches)"
                                )
                            }
                            if "last_enqueued_at" not in batch_columns:
                                connection.execute(
                                    "ALTER TABLE download_batches "
                                    "ADD COLUMN last_enqueued_at REAL"
                                )
                                connection.execute(
                                    "UPDATE download_batches SET last_enqueued_at=updated_at "
                                    "WHERE last_enqueued_at IS NULL"
                                )
                            if "summary_text" not in batch_columns:
                                connection.execute(
                                    "ALTER TABLE download_batches ADD COLUMN summary_text TEXT"
                                )
                            if "created_at" not in batch_columns:
                                connection.execute(
                                    "ALTER TABLE download_batches ADD COLUMN created_at REAL"
                                )
                                connection.execute(
                                    "UPDATE download_batches SET created_at=last_enqueued_at "
                                    "WHERE created_at IS NULL"
                                )
                        self._ready = True
            return connection
        except BaseException:
            connection.close()
            raise

    def _enqueue_sync(self, chat_id, message_id, user_id):
        chat_id, message_id = self._ids(chat_id, message_id)
        user_id = self._user_id(user_id)
        connection = self._connect()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute(
                    "SELECT 1 FROM pending_downloads WHERE chat_id=? AND message_id=?",
                    (chat_id, message_id),
                ).fetchone():
                    return False
                # Only the owner's private chat is eligible for message cleanup.
                batch_id = (
                    self._open_batch(connection, user_id)
                    if chat_id == user_id
                    else None
                )
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO pending_downloads
                       (chat_id,message_id,user_id,state,attempts,last_error_type,updated_at,batch_id)
                       VALUES (?,?,?,'queued',0,NULL,?,?)""",
                    (chat_id, message_id, user_id, time.time(), batch_id),
                )
                return cursor.rowcount == 1
        finally:
            connection.close()

    async def enqueue(self, chat_id, message_id, user_id):
        """Return True only for a new ID; duplicate events never queue twice."""
        return await asyncio.to_thread(self._enqueue_sync, chat_id, message_id, user_id)

    @staticmethod
    def _open_batch(connection, owner_id):
        row = connection.execute(
            "SELECT id,state,last_enqueued_at,created_at FROM download_batches "
            "WHERE owner_id=? "
            "AND state IN ('active','ready') ORDER BY id DESC LIMIT 1",
            (owner_id,),
        ).fetchone()
        now = time.time()
        if (
            row
            and now - (row["last_enqueued_at"] or 0) <= _BATCH_WINDOW_SECONDS
            and now - (row["created_at"] or 0) <= _MAX_BATCH_SPAN_SECONDS
        ):
            connection.execute(
                "UPDATE download_batches SET state='active',updated_at=?,"
                "last_enqueued_at=? WHERE id=?",
                (now, now, row["id"]),
            )
            return row["id"]
        return connection.execute(
            "INSERT INTO download_batches"
            "(owner_id,state,updated_at,last_enqueued_at,created_at,"
            "notification_random_id) VALUES (?,'active',?,?,?,?)",
            (owner_id, now, now, now, secrets.randbits(63)),
        ).lastrowid

    def _initialize_lifecycle_sync(self, owner_id):
        owner_id = self._user_id(owner_id)
        connection = self._connect()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    "SELECT chat_id,message_id FROM pending_downloads WHERE user_id=? "
                    "AND chat_id=? AND batch_id IS NULL AND state!='done'",
                    (owner_id, owner_id),
                ).fetchall()
                if rows:
                    batch_id = self._open_batch(connection, owner_id)
                    connection.execute(
                        "UPDATE pending_downloads SET batch_id=? WHERE user_id=? "
                        "AND chat_id=? AND batch_id IS NULL AND state!='done'",
                        (batch_id, owner_id, owner_id),
                    )
                return len(rows)
        finally:
            connection.close()

    async def initialize_lifecycle(self, owner_id):
        """Attach legacy unfinished work; leave historical completed rows untouched."""
        return await asyncio.to_thread(self._initialize_lifecycle_sync, owner_id)

    def _list_pending_sync(self, recover_running):
        connection = self._connect()
        try:
            with connection:
                if recover_running:
                    # Call at startup, before workers are created. A crashed
                    # running task becomes claimable again without losing attempts.
                    connection.execute(
                        "UPDATE pending_downloads SET state='queued',outcome=NULL,"
                        "updated_at=? "
                        "WHERE state='running'",
                        (time.time(),),
                    )
                    # Failed tasks will be retried after recovery. Keep them
                    # nonterminal while the archive index and workers start.
                    failed = connection.execute(
                        "SELECT chat_id,message_id,user_id,batch_id,last_error_type "
                        "FROM pending_downloads WHERE state='failed'"
                    ).fetchall()
                    for item in failed:
                        if item["last_error_type"] in _TERMINAL_RECOVERY_ERRORS:
                            continue
                        batch_id = item["batch_id"]
                        if batch_id is not None:
                            batch = connection.execute(
                                "SELECT state,summary_text FROM download_batches "
                                "WHERE id=?",
                                (batch_id,),
                            ).fetchone()
                            if batch and batch["state"] in ("claimed", "notified"):
                                if (
                                    batch["state"] == "claimed"
                                    and batch["summary_text"] is None
                                ):
                                    connection.execute(
                                        "UPDATE download_batches SET summary_text=? "
                                        "WHERE id=?",
                                        (
                                            self._completion_text(connection, batch_id),
                                            batch_id,
                                        ),
                                    )
                                batch_id = self._open_batch(
                                    connection, int(item["user_id"])
                                )
                            elif batch and batch["state"] == "ready":
                                connection.execute(
                                    "UPDATE download_batches SET state='active' "
                                    "WHERE id=?",
                                    (batch_id,),
                                )
                        connection.execute(
                            "UPDATE pending_downloads SET state='queued',outcome=NULL,"
                            "updated_at=?,batch_id=? WHERE chat_id=? AND message_id=?",
                            (
                                time.time(),
                                batch_id,
                                item["chat_id"],
                                item["message_id"],
                            ),
                        )
                rows = connection.execute(
                    """SELECT chat_id,message_id,user_id,state,attempts,
                              last_error_type,updated_at
                       FROM pending_downloads WHERE state IN ('queued','running','failed')
                       ORDER BY updated_at,chat_id,message_id"""
                ).fetchall()
                if recover_running:
                    rows = [
                        row
                        for row in rows
                        if row["state"] != "failed"
                        or row["last_error_type"] not in _TERMINAL_RECOVERY_ERRORS
                    ]
                return [dict(row) for row in rows]
        finally:
            connection.close()

    async def list_pending(self, recover_running=True):
        """List unfinished rows; startup recovery changes running to queued.

        The default is intended for one startup call before workers run. Pass
        ``recover_running=False`` for a read-only status snapshot during work.
        """
        return await asyncio.to_thread(self._list_pending_sync, recover_running)

    def _transition_sync(self, chat_id, message_id, sql, parameters=()):
        chat_id, message_id = self._ids(chat_id, message_id)
        connection = self._connect()
        try:
            with connection:
                cursor = connection.execute(
                    sql,
                    (*parameters, time.time(), chat_id, message_id),
                )
                return cursor.rowcount == 1
        finally:
            connection.close()

    async def mark_running(self, chat_id, message_id):
        """Claim a queued or previously failed row and increment attempts."""
        return await asyncio.to_thread(self._mark_running_sync, chat_id, message_id)

    def _mark_running_sync(self, chat_id, message_id):
        chat_id, message_id = self._ids(chat_id, message_id)
        connection = self._connect()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT state,batch_id,user_id FROM pending_downloads "
                    "WHERE chat_id=? AND message_id=?",
                    (chat_id, message_id),
                ).fetchone()
                if row is None or row["state"] not in ("queued", "failed"):
                    return False
                batch_id = row["batch_id"]
                if batch_id is not None:
                    batch = connection.execute(
                        "SELECT state FROM download_batches WHERE id=?", (batch_id,)
                    ).fetchone()
                    if batch and batch["state"] in ("claimed", "notified"):
                        batch_id = self._open_batch(connection, int(row["user_id"]))
                    elif batch and batch["state"] == "ready":
                        connection.execute(
                            "UPDATE download_batches SET state='active' WHERE id=?",
                            (batch_id,),
                        )
                connection.execute(
                    "UPDATE pending_downloads SET state='running',"
                    "attempts=attempts+1,last_error_type=NULL,outcome=NULL,"
                    "updated_at=?,batch_id=? WHERE chat_id=? AND message_id=?",
                    (time.time(), batch_id, chat_id, message_id),
                )
                return True
        finally:
            connection.close()

    async def mark_queued(self, chat_id, message_id):
        """Keep an interrupted transfer pending across a controlled shutdown."""
        return await asyncio.to_thread(
            self._transition_sync,
            chat_id,
            message_id,
            """UPDATE pending_downloads SET state='queued',outcome=NULL,updated_at=?
               WHERE chat_id=? AND message_id=? AND state='running'""",
        )

    async def mark_done(self, chat_id, message_id, outcome="downloaded"):
        """Mark a completed or deduplicated download. Errors propagate."""
        if outcome not in ("downloaded", "skipped"):
            raise ValueError("outcome must be downloaded or skipped")
        return await asyncio.to_thread(
            self._mark_done_sync, chat_id, message_id, outcome
        )

    def _mark_done_sync(self, chat_id, message_id, outcome):
        chat_id, message_id = self._ids(chat_id, message_id)
        connection = self._connect()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT state,batch_id FROM pending_downloads WHERE chat_id=? AND message_id=?",
                    (chat_id, message_id),
                ).fetchone()
                if row is None or row["state"] == "done":
                    return False
                now = time.time()
                connection.execute(
                    "UPDATE pending_downloads SET state='done',last_error_type=NULL,"
                    "outcome=?,updated_at=? "
                    "WHERE chat_id=? AND message_id=?",
                    (outcome, now, chat_id, message_id),
                )
                batch_id = row["batch_id"]
                if batch_id is not None:
                    connection.execute(
                        "INSERT OR IGNORE INTO download_cleanup"
                        "(chat_id,message_id,batch_id,done_at) VALUES (?,?,?,?)",
                        (chat_id, message_id, batch_id, now),
                    )
                    self._settle_batch(connection, batch_id, now)
                return True
        finally:
            connection.close()

    @staticmethod
    def _settle_batch(connection, batch_id, now):
        if connection.execute(
            "SELECT 1 FROM pending_downloads WHERE batch_id=? "
            "AND state IN ('queued','running') LIMIT 1",
            (batch_id,),
        ).fetchone():
            return
        connection.execute(
            "UPDATE download_batches SET state='ready',updated_at=? "
            "WHERE id=? AND state='active'",
            (now, batch_id),
        )

    def _list_cleanup_sync(self, owner_id, limit):
        owner_id = self._user_id(owner_id)
        connection = self._connect()
        try:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT c.chat_id,c.message_id,c.batch_id FROM download_cleanup c "
                    "JOIN download_batches b ON b.id=c.batch_id WHERE b.owner_id=? "
                    "AND c.acked_at IS NULL ORDER BY c.done_at,c.chat_id,c.message_id LIMIT ?",
                    (owner_id, max(1, min(1000, int(limit)))),
                ).fetchall()
            ]
        finally:
            connection.close()

    async def list_cleanup(self, owner_id, limit=100):
        return await asyncio.to_thread(self._list_cleanup_sync, owner_id, limit)

    def _ack_cleanup_sync(self, chat_id, message_id):
        chat_id, message_id = self._ids(chat_id, message_id)
        connection = self._connect()
        try:
            with connection:
                return (
                    connection.execute(
                        "UPDATE download_cleanup SET acked_at=? WHERE chat_id=? AND message_id=? "
                        "AND acked_at IS NULL",
                        (time.time(), chat_id, message_id),
                    ).rowcount
                    == 1
                )
        finally:
            connection.close()

    async def ack_cleanup(self, chat_id, message_id):
        return await asyncio.to_thread(self._ack_cleanup_sync, chat_id, message_id)

    def _completion_text(self, connection, batch_id):
        row = connection.execute(
            """SELECT count(*) AS total,
                      sum(CASE WHEN state='done' AND outcome='downloaded' THEN 1 ELSE 0 END) AS downloaded,
                      sum(CASE WHEN state='done' AND outcome='skipped' THEN 1 ELSE 0 END) AS skipped,
                      sum(CASE WHEN state='done' AND outcome IS NULL THEN 1 ELSE 0 END) AS unknown,
                      sum(CASE WHEN state='failed' THEN 1 ELSE 0 END) AS failed
               FROM pending_downloads WHERE batch_id=?""",
            (batch_id,),
        ).fetchone()
        total = int(row["total"])
        downloaded = int(row["downloaded"])
        skipped = int(row["skipped"])
        unknown = int(row["unknown"])
        failed = int(row["failed"])
        if total != downloaded + skipped + unknown + failed:
            raise ValueError("Batch contains unsettled or invalid outcomes")
        title = self.messages.text(
            "summary.failure_title" if failed else "summary.success_title"
        )
        lines = [
            title,
            self.messages.text(
                "summary.counts",
                total=total,
                downloaded=downloaded,
                skipped=skipped,
                failed=failed,
            ),
        ]
        if unknown:
            lines.append(self.messages.text("summary.legacy_unknown", count=unknown))
        return "\n".join(lines)

    def _next_completion_sync(self, owner_id, quiet_seconds):
        owner_id = self._user_id(owner_id)
        connection = self._connect()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                # Retry a claimed notice with the same Telegram random_id after a restart.
                row = connection.execute(
                    "SELECT id,owner_id,notification_random_id,summary_text FROM download_batches "
                    "WHERE owner_id=? AND state='claimed' ORDER BY id LIMIT 1",
                    (owner_id,),
                ).fetchone()
                if row:
                    if row["summary_text"] is None:
                        summary = self._completion_text(connection, row["id"])
                        connection.execute(
                            "UPDATE download_batches SET summary_text=? WHERE id=?",
                            (summary, row["id"]),
                        )
                        return {**dict(row), "summary_text": summary}
                    return dict(row)
                for batch in connection.execute(
                    "SELECT id,owner_id,notification_random_id,last_enqueued_at "
                    "FROM download_batches "
                    "WHERE owner_id=? AND state='ready' ORDER BY id",
                    (owner_id,),
                ).fetchall():
                    if time.time() - (batch["last_enqueued_at"] or 0) < quiet_seconds:
                        continue
                    if connection.execute(
                        "SELECT 1 FROM pending_downloads WHERE batch_id=? "
                        "AND state IN ('queued','running') LIMIT 1",
                        (batch["id"],),
                    ).fetchone():
                        continue
                    summary = self._completion_text(connection, batch["id"])
                    connection.execute(
                        "UPDATE download_batches SET state='claimed',summary_text=? "
                        "WHERE id=? AND state='ready'",
                        (summary, batch["id"]),
                    )
                    return {
                        **{
                            key: batch[key]
                            for key in ("id", "owner_id", "notification_random_id")
                        },
                        "summary_text": summary,
                    }
                return None
        finally:
            connection.close()

    async def next_completion(self, owner_id, quiet_seconds=3):
        """Claim a settled batch once its arrival window has passed."""
        return await asyncio.to_thread(
            self._next_completion_sync, owner_id, quiet_seconds
        )

    def _ack_completion_sync(self, batch_id):
        connection = self._connect()
        try:
            with connection:
                return (
                    connection.execute(
                        "UPDATE download_batches SET state='notified',notified_at=? "
                        "WHERE id=? AND state='claimed'",
                        (time.time(), int(batch_id)),
                    ).rowcount
                    == 1
                )
        finally:
            connection.close()

    async def ack_completion(self, batch_id):
        return await asyncio.to_thread(self._ack_completion_sync, batch_id)

    async def mark_failed(self, chat_id, message_id, error_type=None):
        """Keep a failed task recoverable; store only an exception class name."""
        name = self._error_name(error_type)
        return await asyncio.to_thread(
            self._mark_failed_sync, chat_id, message_id, name
        )

    def _mark_failed_sync(self, chat_id, message_id, name):
        chat_id, message_id = self._ids(chat_id, message_id)
        connection = self._connect()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT state,batch_id FROM pending_downloads "
                    "WHERE chat_id=? AND message_id=?",
                    (chat_id, message_id),
                ).fetchone()
                if row is None or row["state"] == "done":
                    return False
                now = time.time()
                connection.execute(
                    "UPDATE pending_downloads SET state='failed',last_error_type=?,"
                    "outcome='failed',updated_at=? WHERE chat_id=? AND message_id=?",
                    (name, now, chat_id, message_id),
                )
                if row["batch_id"] is not None:
                    self._settle_batch(connection, row["batch_id"], now)
                return True
        finally:
            connection.close()
