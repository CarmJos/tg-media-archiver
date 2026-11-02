"""Durable identifiers for Telegram downloads awaiting a worker.

The queue shares ``files.db`` with the media deduplication index and owns
``pending_downloads``, ``download_batches``, and ``download_cleanup``.
"""

import asyncio
import os
import re
import sqlite3
import threading
import time
import secrets


_ERROR_TYPE = re.compile(r"^[A-Za-z_][A-Za-z_0-9]{0,127}$")


class PersistentQueue:
    """SQLite queue keyed by the Bot chat ID and message ID."""

    def __init__(self, db_path):
        self.db_path = os.path.abspath(os.fspath(db_path))
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
            "SELECT id,state FROM download_batches WHERE owner_id=? "
            "AND state IN ('active','ready') ORDER BY id DESC LIMIT 1",
            (owner_id,),
        ).fetchone()
        now = time.time()
        if row:
            connection.execute(
                "UPDATE download_batches SET state='active',updated_at=? WHERE id=?",
                (now, row["id"]),
            )
            return row["id"]
        return connection.execute(
            "INSERT INTO download_batches(owner_id,state,updated_at,notification_random_id) "
            "VALUES (?,'active',?,?)",
            (owner_id, now, secrets.randbits(63)),
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
                        "UPDATE pending_downloads SET state='queued',updated_at=? "
                        "WHERE state='running'",
                        (time.time(),),
                    )
                rows = connection.execute(
                    """SELECT chat_id,message_id,user_id,state,attempts,
                              last_error_type,updated_at
                       FROM pending_downloads WHERE state IN ('queued','running','failed')
                       ORDER BY updated_at,chat_id,message_id"""
                ).fetchall()
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
        return await asyncio.to_thread(
            self._transition_sync,
            chat_id,
            message_id,
            """UPDATE pending_downloads
               SET state='running', attempts=attempts+1,
                   last_error_type=NULL, updated_at=?
               WHERE chat_id=? AND message_id=? AND state IN ('queued','failed')""",
        )

    async def mark_done(self, chat_id, message_id):
        """Mark a completed or deduplicated download. Errors propagate."""
        return await asyncio.to_thread(self._mark_done_sync, chat_id, message_id)

    def _mark_done_sync(self, chat_id, message_id):
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
                    "UPDATE pending_downloads SET state='done',last_error_type=NULL,updated_at=? "
                    "WHERE chat_id=? AND message_id=?",
                    (now, chat_id, message_id),
                )
                batch_id = row["batch_id"]
                if batch_id is not None:
                    connection.execute(
                        "INSERT OR IGNORE INTO download_cleanup"
                        "(chat_id,message_id,batch_id,done_at) VALUES (?,?,?,?)",
                        (chat_id, message_id, batch_id, now),
                    )
                    remaining = connection.execute(
                        "SELECT 1 FROM pending_downloads WHERE batch_id=? AND state!='done' LIMIT 1",
                        (batch_id,),
                    ).fetchone()
                    if not remaining:
                        connection.execute(
                            "UPDATE download_batches SET state='ready',updated_at=? "
                            "WHERE id=? AND state='active'",
                            (now, batch_id),
                        )
                return True
        finally:
            connection.close()

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

    def _next_completion_sync(self, owner_id, quiet_seconds):
        owner_id = self._user_id(owner_id)
        connection = self._connect()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                # Retry a claimed notice with the same Telegram random_id after a restart.
                row = connection.execute(
                    "SELECT id,owner_id,notification_random_id FROM download_batches "
                    "WHERE owner_id=? AND state='claimed' ORDER BY id LIMIT 1",
                    (owner_id,),
                ).fetchone()
                if row:
                    return dict(row)
                for batch in connection.execute(
                    "SELECT id,owner_id,notification_random_id,updated_at FROM download_batches "
                    "WHERE owner_id=? AND state='ready' ORDER BY id",
                    (owner_id,),
                ).fetchall():
                    if time.time() - batch["updated_at"] < quiet_seconds:
                        continue
                    if connection.execute(
                        "SELECT 1 FROM pending_downloads WHERE batch_id=? AND state!='done' LIMIT 1",
                        (batch["id"],),
                    ).fetchone():
                        continue
                    count = connection.execute(
                        "SELECT count(*) FROM pending_downloads WHERE batch_id=?",
                        (batch["id"],),
                    ).fetchone()[0]
                    if count > 1:
                        connection.execute(
                            "UPDATE download_batches SET state='claimed' WHERE id=? AND state='ready'",
                            (batch["id"],),
                        )
                        return {
                            key: batch[key]
                            for key in ("id", "owner_id", "notification_random_id")
                        }
                    connection.execute(
                        "UPDATE download_batches SET state='notified',notified_at=? WHERE id=?",
                        (time.time(), batch["id"]),
                    )
                return None
        finally:
            connection.close()

    async def next_completion(self, owner_id, quiet_seconds=3):
        """Claim a completed multi-item batch after a short album arrival window."""
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
            self._transition_sync,
            chat_id,
            message_id,
            """UPDATE pending_downloads
               SET state='failed',last_error_type=?,updated_at=?
               WHERE chat_id=? AND message_id=? AND state!='done'""",
            (name,),
        )

    async def requeue(self, chat_id, message_id):
        """Explicitly replay a failed or completed ID; never reset active work."""
        return await asyncio.to_thread(self._requeue_sync, chat_id, message_id)

    def _requeue_sync(self, chat_id, message_id):
        chat_id, message_id = self._ids(chat_id, message_id)
        connection = self._connect()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT user_id,state,batch_id FROM pending_downloads "
                    "WHERE chat_id=? AND message_id=?",
                    (chat_id, message_id),
                ).fetchone()
                if row is None or row["state"] not in ("failed", "done"):
                    return False
                batch_id = row["batch_id"]
                if batch_id is not None:
                    batch = connection.execute(
                        "SELECT state FROM download_batches WHERE id=?", (batch_id,)
                    ).fetchone()
                    if batch and batch["state"] in ("claimed", "notified"):
                        batch_id = self._open_batch(connection, row["user_id"])
                    else:
                        connection.execute(
                            "UPDATE download_batches SET state='active',updated_at=? "
                            "WHERE id=?",
                            (time.time(), batch_id),
                        )
                    connection.execute(
                        "DELETE FROM download_cleanup WHERE chat_id=? AND message_id=?",
                        (chat_id, message_id),
                    )
                return (
                    connection.execute(
                        "UPDATE pending_downloads SET state='queued',batch_id=?,"
                        "last_error_type=NULL,updated_at=? WHERE chat_id=? AND message_id=?",
                        (batch_id, time.time(), chat_id, message_id),
                    ).rowcount
                    == 1
                )
        finally:
            connection.close()

    async def settle(self, chat_id, message_id, outcome, error_type=None):
        """Convenience transition for worker outcome names or enum members."""
        name = getattr(outcome, "name", outcome)
        if name in ("success", "skip", "SuccessDownload", "SkipDownload"):
            return await self.mark_done(chat_id, message_id)
        if name in ("failed", "FailedDownload"):
            return await self.mark_failed(chat_id, message_id, error_type)
        raise ValueError("unsupported queue outcome")

    def _import_pending_sync(self, rows):
        normalized = []
        for row in rows:
            if isinstance(row, dict):
                raw = (row["chat_id"], row["message_id"], row["user_id"])
            else:
                raw = tuple(row)
            if len(raw) != 3:
                raise ValueError("each imported row needs chat_id,message_id,user_id")
            normalized.append((*self._ids(raw[0], raw[1]), self._user_id(raw[2])))
        connection = self._connect()
        try:
            inserted = 0
            with connection:
                for chat_id, message_id, user_id in normalized:
                    cursor = connection.execute(
                        """INSERT OR IGNORE INTO pending_downloads
                           (chat_id,message_id,user_id,state,attempts,last_error_type,updated_at)
                           VALUES (?,?,?,'queued',0,NULL,?)""",
                        (chat_id, message_id, user_id, time.time()),
                    )
                    inserted += cursor.rowcount
            return inserted
        finally:
            connection.close()

    async def import_pending(self, rows):
        """Import ID triples atomically without resetting existing rows."""
        return await asyncio.to_thread(self._import_pending_sync, list(rows))
