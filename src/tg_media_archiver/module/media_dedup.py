"""Persistent, content-verified deduplication for the local downloads share.

Only regular files below ``root`` are indexed. Database failures are fail-open:
they disable a dedup hit, never a real download or final file save.
"""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import logging
import os
import sqlite3
import stat
import threading

logger = logging.getLogger(__name__)


class MediaDedup:
    """Index Telegram identity and SHA-256 to one or more verified local paths."""

    def __init__(self, root):
        self.root = os.path.abspath(os.fspath(root))
        self.root_real = os.path.realpath(self.root)
        self.db_path = os.path.join(self.root, "files.db")
        self._ready = False
        self._schema_lock = threading.Lock()
        self._locks = {}

    @staticmethod
    def identity(kind, file_unique_id, media_size=0):
        if not kind or not file_unique_id:
            return None
        return (str(kind), str(file_unique_id), int(media_size or 0))

    @asynccontextmanager
    async def lock_for(self, key):
        """Reference-counted in-process lock; all waiters share the same lock."""
        entry = self._locks.get(key)
        if entry is None:
            entry = [asyncio.Lock(), 0]
            self._locks[key] = entry
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0 and self._locks.get(key) is entry:
                self._locks.pop(key, None)

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            if not self._ready:
                with self._schema_lock:
                    if not self._ready:
                        connection.executescript(
                            """
                        CREATE TABLE IF NOT EXISTS files (
                            path TEXT PRIMARY KEY,
                            sha256 TEXT NOT NULL,
                            size INTEGER NOT NULL,
                            dev INTEGER NOT NULL,
                            ino INTEGER NOT NULL,
                            mtime_ns INTEGER NOT NULL,
                            ctime_ns INTEGER NOT NULL
                        );
                        CREATE INDEX IF NOT EXISTS files_by_hash ON files(sha256);
                        CREATE TABLE IF NOT EXISTS identities (
                            kind TEXT NOT NULL,
                            unique_id TEXT NOT NULL,
                            media_size INTEGER NOT NULL,
                            sha256 TEXT NOT NULL,
                            path TEXT NOT NULL,
                            PRIMARY KEY(kind, unique_id, media_size)
                        );
                        CREATE INDEX IF NOT EXISTS identities_by_hash ON identities(sha256);
                        """
                        )
                        self._ready = True
            return connection
        except BaseException:
            connection.close()
            raise

    def _signature(self, path):
        """Reject DB files, symlinks, non-regular files, and root escapes."""
        path = os.path.abspath(os.fspath(path))
        try:
            if os.path.commonpath((self.root, path)) != self.root:
                return None
            if (
                os.path.commonpath((self.root_real, os.path.realpath(path)))
                != self.root_real
            ):
                return None
            name = os.path.basename(path)
            if path == self.db_path or name.startswith("files.db-"):
                return None
            if name.startswith(".tdl-") and name.endswith(".part"):
                return None
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode):
                return None
        except (OSError, ValueError):
            return None
        return (
            int(info.st_dev),
            int(info.st_ino),
            int(info.st_size),
            int(info.st_mtime_ns),
            int(info.st_ctime_ns),
        )

    def _hash_file(self, path):
        before = self._signature(path)
        if before is None:
            return None
        digest = hashlib.sha256()
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
        except OSError:
            return None
        after = self._signature(path)
        if after != before:
            return None
        return digest.hexdigest(), after

    @staticmethod
    def _row_signature(row):
        return (
            int(row["dev"]),
            int(row["ino"]),
            row["size"],
            row["mtime_ns"],
            row["ctime_ns"],
        )

    def _upsert_file(self, connection, path, sha256, signature):
        connection.execute(
            """INSERT INTO files(path,sha256,size,dev,ino,mtime_ns,ctime_ns)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
               sha256=excluded.sha256,size=excluded.size,dev=excluded.dev,
               ino=excluded.ino,mtime_ns=excluded.mtime_ns,ctime_ns=excluded.ctime_ns""",
            (
                path,
                sha256,
                signature[2],
                str(signature[0]),
                str(signature[1]),
                signature[3],
                signature[4],
            ),
        )
        # An external edit must never silently bind an identity to the new bytes.
        connection.execute(
            "DELETE FROM identities WHERE path=? AND sha256<>?", (path, sha256)
        )

    def _verified_digest(self, connection, path):
        path = os.path.abspath(os.fspath(path))
        signature = self._signature(path)
        if signature is None:
            return None
        row = connection.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
        if row and self._row_signature(row) == signature:
            return row["sha256"]
        hashed = self._hash_file(path)
        if hashed is None:
            return None
        sha256, signature = hashed
        with connection:
            self._upsert_file(connection, path, sha256, signature)
        return sha256

    def _find_hash(self, connection, sha256):
        rows = connection.execute(
            "SELECT path FROM files WHERE sha256=?", (sha256,)
        ).fetchall()
        for row in rows:
            path = row["path"]
            if self._verified_digest(connection, path) == sha256:
                return path
        return None

    def _find_identity_sync(self, identity):
        if identity is None:
            return None
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT sha256,path FROM identities WHERE kind=? AND unique_id=? AND media_size=?",
                identity,
            ).fetchone()
            if row is None:
                return None
            if self._verified_digest(connection, row["path"]) == row["sha256"]:
                return row["path"]
            return self._find_hash(connection, row["sha256"])
        finally:
            connection.close()

    async def find_identity(self, identity):
        try:
            return await asyncio.to_thread(self._find_identity_sync, identity)
        except (OSError, sqlite3.Error) as exc:
            logger.warning(
                "media dedup identity lookup unavailable: %s", type(exc).__name__
            )
            return None

    def _find_hash_sync(self, sha256):
        connection = self._connect()
        try:
            return self._find_hash(connection, sha256)
        finally:
            connection.close()

    async def find_hash(self, sha256):
        try:
            return await asyncio.to_thread(self._find_hash_sync, sha256)
        except (OSError, sqlite3.Error) as exc:
            logger.warning(
                "media dedup hash lookup unavailable: %s", type(exc).__name__
            )
            return None

    async def hash_file(self, path):
        return await asyncio.to_thread(self._hash_file, path)

    @staticmethod
    def _hash_temp_sync(path, temp_root):
        path = os.path.abspath(os.fspath(path))
        temp_root = os.path.abspath(os.fspath(temp_root))
        try:
            if os.path.commonpath((temp_root, path)) != temp_root:
                return None
            if os.path.commonpath(
                (os.path.realpath(temp_root), os.path.realpath(path))
            ) != os.path.realpath(temp_root):
                return None
            before = os.lstat(path)
            if not stat.S_ISREG(before.st_mode):
                return None
            digest = hashlib.sha256()
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            after = os.lstat(path)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                return None
            return digest.hexdigest()
        except (OSError, ValueError):
            return None

    async def hash_temp(self, path, temp_root):
        return await asyncio.to_thread(self._hash_temp_sync, path, temp_root)

    def _record_sync(self, path, sha256, signature, identity):
        path = os.path.abspath(os.fspath(path))
        if self._signature(path) != signature:
            return False
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._upsert_file(connection, path, sha256, signature)
            if identity is not None:
                connection.execute(
                    """INSERT INTO identities(kind,unique_id,media_size,sha256,path)
                       VALUES(?,?,?,?,?) ON CONFLICT(kind,unique_id,media_size)
                       DO UPDATE SET sha256=excluded.sha256,path=excluded.path""",
                    (*identity, sha256, path),
                )
            connection.commit()
            return True
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def record(self, path, sha256, signature, identity=None):
        try:
            return await asyncio.to_thread(
                self._record_sync, path, sha256, signature, identity
            )
        except (OSError, sqlite3.Error) as exc:
            logger.warning("media dedup record unavailable: %s", type(exc).__name__)
            return False

    async def register_existing(self, path, identity=None):
        """Bootstrap one existing file; identity=(kind, unique_id, media_size) is optional."""
        hashed = await self.hash_file(path)
        if hashed is None:
            return False
        return await self.record(path, hashed[0], hashed[1], identity)

    async def bind_identity(self, kind, file_unique_id, media_size, path):
        """Bind a Telegram identity to already saved bytes after online mapping."""
        identity = self.identity(kind, file_unique_id, media_size)
        if identity is None:
            return False
        return await self.register_existing(path, identity)

    async def bootstrap_existing(self):
        """Index current regular files; never follow symlink directories."""
        indexed = 0
        for directory, subdirs, files in os.walk(self.root, followlinks=False):
            subdirs[:] = [
                name
                for name in subdirs
                if not os.path.islink(os.path.join(directory, name))
            ]
            for name in files:
                if await self.register_existing(os.path.join(directory, name)):
                    indexed += 1
        return indexed

    async def verified_path_digest(self, path):
        """Hash a target path even if the database is unavailable."""
        hashed = await self.hash_file(path)
        return hashed[0] if hashed else None

    @staticmethod
    def collision_path(path, sha256):
        """Choose a new name without overwriting an existing final file."""
        parent, name = os.path.split(path)
        stem, suffix = os.path.splitext(name)
        candidate = os.path.join(parent, f"{stem}-{sha256[:12]}{suffix}")
        counter = 2
        while os.path.lexists(candidate):
            candidate = os.path.join(parent, f"{stem}-{sha256[:12]}-{counter}{suffix}")
            counter += 1
        return candidate
