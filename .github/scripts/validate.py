"""Offline smoke checks for an installed tg-media-archiver wheel."""

import asyncio
import os
import pkgutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from importlib import import_module, metadata, resources
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def check_installation():
    package = import_module("tg_media_archiver")
    checkout = Path(__file__).resolve().parents[2]
    installed_at = Path(package.__file__).resolve()
    assert not installed_at.is_relative_to(checkout), (
        f"Imported source checkout instead of installed wheel: {installed_at}"
    )

    modules = list(pkgutil.walk_packages(package.__path__, package.__name__ + "."))
    for module in modules:
        import_module(module.name)
    assert modules, "No installed Python modules were found"

    sample = resources.files(package).joinpath("config.example.yaml")
    assert sample.is_file(), "Packaged sample configuration is missing"

    for argument, expected in (
        ("--help", "configure"),
        ("--version", metadata.version("tg-media-archiver")),
    ):
        result = subprocess.run(
            [sys.executable, "-m", "tg_media_archiver", argument],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert expected in result.stdout, f"CLI {argument} returned unexpected output"

    import tgcrypto

    payload = bytes(range(32))
    key = bytes(range(32))
    iv = bytes(reversed(range(32)))
    encrypted = tgcrypto.ige256_encrypt(payload, key, iv)
    assert encrypted != payload
    assert tgcrypto.ige256_decrypt(encrypted, key, iv) == payload

    print(
        f"Installed wheel {metadata.version('tg-media-archiver')}: "
        f"{len(modules)} modules, CLI, sample config and encryption passed"
    )


def check_configuration():
    from ruamel.yaml import YAML

    from tg_media_archiver.configure import configure
    from tg_media_archiver.module.app import Application

    credentials = ["1", "a" * 32, "1:" + "b" * 35]
    with patch("getpass.getpass", side_effect=credentials):
        configure()
    config = YAML().load(Path("config.yaml").read_text(encoding="utf-8"))
    assert config["api_id"] == 1
    assert config["api_hash"] == credentials[1]
    assert config["bot_token"] == credentials[2]
    assert config["allowed_user_ids"] == []

    config["allowed_user_ids"] = [1]
    with Path("config.yaml").open("w", encoding="utf-8") as stream:
        YAML().dump(config, stream)
    app = Application("config.yaml")
    app.load_config()
    assert app.allowed_user_ids == [1]
    assert app.max_download_task == 3
    assert app.max_concurrent_transmissions == 2
    print("Hidden configuration and offline loading passed")


def check_message_catalog(directory):
    from ruamel.yaml import YAML

    from tg_media_archiver.module.app import Application
    from tg_media_archiver.module.messages import Messages

    package = resources.files("tg_media_archiver.messages")
    for language in ("zh_CN", "en_US"):
        assert package.joinpath(f"{language}.yml").is_file(), (
            f"Packaged {language} messages are missing"
        )
    assert Messages().text("bot.command_help") == "查看使用说明"
    assert Messages("en_US").text("bot.command_help") == "Show help"

    config = YAML().load(Path("config.yaml").read_text(encoding="utf-8"))
    locale_root = directory / "localization"
    locale_root.mkdir()
    config_path = locale_root / "config.yaml"
    config.update(language="en_US", messages_dir="messages")
    with config_path.open("w", encoding="utf-8") as stream:
        YAML().dump(config, stream)
    messages_dir = locale_root / "messages"
    messages_dir.mkdir()
    (messages_dir / "en_US.yml").write_text(
        'bot:\n  command_help: "English override"\n', encoding="utf-8"
    )
    app = Application(config_path)
    app.load_config()
    assert app.messages.language == "en_US"
    assert app.messages.text("bot.command_help") == "English override"
    assert app.messages.text("bot.command_progress") == "View download progress"

    env_dir = locale_root / "env-messages"
    env_dir.mkdir()
    (env_dir / "zh_CN.yml").write_text(
        'bot:\n  command_help: "Environment override"\n', encoding="utf-8"
    )
    with patch.dict(
        os.environ,
        {"TGA_LANGUAGE": "zh_CN", "TGA_MESSAGES_DIR": "env-messages"},
    ):
        app.load_config()
        assert app.messages.language == "zh_CN"
        assert app.messages.text("bot.command_help") == "Environment override"
        assert app.messages.text("bot.command_progress") == "查看或关闭下载进度"

    (messages_dir / "fr_FR.yml").write_text(
        'bot:\n  command_help: "Aide"\n', encoding="utf-8"
    )
    french = Messages("fr_FR", messages_dir)
    assert french.text("bot.command_help") == "Aide"
    assert french.text("bot.command_progress") == "View download progress"

    invalid_catalogs = (
        'progress:\n  queue_stats: "{unknown}"\n',
        'progress:\n  queue_stats: "{total"\n',
        'bot:\n  nonexistent: "wrong key"\n',
    )
    for content in invalid_catalogs:
        (messages_dir / "en_US.yml").write_text(content, encoding="utf-8")
        try:
            Messages("en_US", messages_dir)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Invalid message catalog was accepted: {content}")
    for language in ("en-US", "fr_FR"):
        try:
            Messages(language, None)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Invalid or unavailable locale accepted: {language}")
    print("Packaged bilingual messages, overrides and validation passed")


async def check_storage(directory):
    from tg_media_archiver.module.media_dedup import MediaDedup
    from tg_media_archiver.module.persistent_queue import PersistentQueue
    from tg_media_archiver.module.share_files import publish_download

    share = directory / "downloads"
    share.mkdir()
    source = directory / "incoming.bin"
    contents = b"offline media verification"
    source.write_bytes(contents)
    destination = share / "nested" / "media.bin"
    publish_download(source, destination, share)
    assert destination.read_bytes() == contents and not source.exists()
    assert destination.parent.is_dir()
    if os.name == "posix":
        assert destination.stat().st_mode & 0o600 == 0o600
        assert destination.parent.stat().st_mode & 0o700 == 0o700

    index = MediaDedup(share)
    identity = index.identity("document", "offline-media-1", len(contents))
    assert await index.register_existing(destination, identity)
    assert Path(await index.find_identity(identity)) == destination
    destination.write_bytes(b"changed media")
    assert await index.find_identity(identity) is None

    second = share / "nested" / "removed.bin"
    second.write_bytes(contents)
    second_identity = index.identity("document", "offline-media-2", len(contents))
    assert await index.register_existing(second, second_identity)
    second.unlink()
    assert await index.find_identity(second_identity) is None

    imported = share / "imported.bin"
    imported.write_bytes(contents)
    await index.refresh_index()
    assert Path(await index.find_identity(identity)) == imported
    assert Path(await index.find_identity(second_identity)) == imported
    moved = share / "renamed.bin"
    imported.rename(moved)
    await index.refresh_index()
    assert Path(await index.find_identity(identity)) == moved
    with patch.object(
        index, "_hash_file", side_effect=AssertionError("Unchanged file rehashed")
    ):
        assert (await index.refresh_index())["indexed"] == 0
    moved.unlink()
    await index.refresh_index()
    assert await index.find_identity(identity) is None

    queue = PersistentQueue(share / "files.db")
    assert await queue.enqueue(1, 10, 1)
    assert not await queue.enqueue(1, 10, 1)
    assert await queue.mark_running(1, 10)
    restarted = PersistentQueue(share / "files.db")
    pending = await restarted.list_pending(recover_running=True)
    assert len(pending) == 1 and pending[0]["state"] == "queued"
    await restarted.mark_done(1, 10)
    assert not await restarted.list_pending()
    cleanup = await restarted.list_cleanup(1)
    assert len(cleanup) == 1
    await restarted.ack_cleanup(1, 10)
    assert not await restarted.list_cleanup(1)

    legacy = directory / "legacy"
    legacy.mkdir()
    legacy_db = legacy / "files.db"
    with closing(sqlite3.connect(legacy_db)) as connection, connection:
        connection.execute(
            "CREATE TABLE pending_downloads (chat_id INTEGER, message_id INTEGER, "
            "user_id INTEGER, state TEXT, attempts INTEGER, "
            "last_error_type TEXT, updated_at REAL, "
            "PRIMARY KEY(chat_id, message_id))"
        )
        connection.execute(
            "INSERT INTO pending_downloads VALUES (1,70,1,'running',2,NULL,1)"
        )
    legacy_queue = PersistentQueue(legacy_db)
    assert await legacy_queue.initialize_lifecycle(1) == 1
    migrated = await legacy_queue.list_pending(recover_running=True)
    assert len(migrated) == 1 and migrated[0]["message_id"] == 70
    assert migrated[0]["state"] == "queued" and migrated[0]["attempts"] == 2
    assert await legacy_queue.mark_running(1, 70)
    assert await legacy_queue.mark_done(1, 70)
    assert (await legacy_queue.list_cleanup(1))[0]["message_id"] == 70
    print("File publishing, deduplication, queue recovery and cleanup passed")


async def check_private_intake(directory):
    from datetime import datetime, timezone

    from pyrogram.enums import ChatType, MessageMediaType

    from tg_media_archiver.module.app import Application
    from tg_media_archiver.module.bot import DownloadBot, _is_authorized_forward

    app = Application("config.yaml")
    app.load_config()
    app.save_path = str(directory / "intake-downloads")
    app.pre_run()
    accepted = []

    async def enqueue(message, node):
        accepted.append((message, node))

    bot = DownloadBot(app, enqueue)
    bot.client = FakeTelegram()

    def forwarded(message_id, media_type, album=None):
        kind = MessageMediaType(media_type)
        item = SimpleNamespace(
            file_name=f"item-{message_id}.{'mp4' if media_type == 'video' else 'jpg'}",
            file_unique_id=f"unique-{message_id}",
            file_size=12,
        )
        message = SimpleNamespace(
            id=message_id,
            from_user=SimpleNamespace(id=1),
            chat=SimpleNamespace(id=1, type=ChatType.PRIVATE),
            forward_date=datetime.now(timezone.utc),
            media=kind,
            media_group_id=album,
            caption=None,
        )
        setattr(message, media_type, item)
        return message

    photo = forwarded(30, "photo")
    video = forwarded(31, "video")
    album = [forwarded(32, "photo", "album-1"), forwarded(33, "video", "album-1")]
    for message in [photo, video, *album]:
        assert _is_authorized_forward(message, 1)
        assert await bot._enqueue_message(message)
    assert not await bot._enqueue_message(photo)
    assert len(accepted) == 4
    assert len(await bot.persistent_queue.list_pending(recover_running=False)) == 4

    other_user = forwarded(34, "photo")
    other_user.from_user.id = 2
    group = forwarded(35, "video")
    group.chat.type = ChatType.SUPERGROUP
    unforwarded = forwarded(36, "photo")
    unforwarded.forward_date = None
    for rejected in (other_user, group, unforwarded):
        assert not _is_authorized_forward(rejected, 1)
        assert not await bot._enqueue_message(rejected)
    assert len(accepted) == 4

    startup_app = Application("config.yaml")
    startup_app.load_config()
    startup_app.save_path = str(directory / "startup-downloads")
    startup_app.pre_run()
    startup_accepted = []

    async def startup_enqueue(message, _node):
        startup_accepted.append(message.id)

    startup_bot = DownloadBot(startup_app, startup_enqueue)
    startup_bot.client = FakeTelegram()
    early = forwarded(37, "video")
    startup_bot.client.messages[early.id] = early
    await startup_bot._on_forward(startup_bot.client, early)
    assert not startup_accepted and len(startup_bot._startup_buffer) == 1
    before_ready = await startup_bot.persistent_queue.list_pending(
        recover_running=False
    )
    assert [row["message_id"] for row in before_ready] == [37]
    await startup_bot._recover_pending()
    for buffered in startup_bot._startup_buffer:
        await startup_bot._enqueue_message(buffered, recovered=True)
    assert startup_accepted == [37]
    await startup_bot.persistent_queue.mark_done(1, 37)
    await startup_bot.persistent_queue.enqueue(1, 38, 1)
    with patch.object(startup_bot.client, "get_messages", side_effect=OSError):
        await startup_bot._recover_pending()
    unavailable = await startup_bot.persistent_queue.list_pending(recover_running=False)
    assert len(unavailable) == 1 and unavailable[0]["last_error_type"] == "OSError"
    retryable = await startup_bot.persistent_queue.list_pending(recover_running=True)
    assert len(retryable) == 1 and retryable[0]["state"] == "queued"
    print("Private owner-only photo, video and mixed-album intake passed")


class FakeTelegram:
    """Small local stand-in for the messages used by progress and cleanup."""

    def __init__(self, owner_id=1):
        self.owner_id = owner_id
        self.next_id = 100
        self.messages = {}
        self.edits = []
        self.deleted = []
        self.invoked = []
        self.downloaded = []
        self.fail_download_ids = set()
        self.cancel_download_ids = set()

    async def send_message(self, chat_id, text, **kwargs):
        assert chat_id == self.owner_id
        self.next_id += 1
        message = SimpleNamespace(
            id=self.next_id,
            text=text,
            chat=SimpleNamespace(id=chat_id, type=SimpleNamespace(value="private")),
            from_user=SimpleNamespace(id=999),
            reply_markup=kwargs.get("reply_markup"),
        )
        self.messages[message.id] = message
        return message

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        assert chat_id == self.owner_id
        self.edits.append((message_id, text))
        self.messages[message_id].text = text
        self.messages[message_id].reply_markup = kwargs.get("reply_markup")
        return self.messages[message_id]

    async def delete_messages(self, chat_id, message_id, revoke=False):
        assert chat_id == self.owner_id and revoke is True
        for item_id in message_id if isinstance(message_id, list) else [message_id]:
            self.deleted.append(item_id)
            self.messages.pop(item_id, None)

    async def get_messages(self, chat_id, message_id, **_kwargs):
        assert chat_id == self.owner_id
        if isinstance(message_id, list):
            return [self.messages.get(item_id) for item_id in message_id]
        return self.messages.get(message_id)

    async def resolve_peer(self, owner_id):
        from pyrogram import raw

        assert owner_id == self.owner_id
        return raw.types.InputPeerSelf()

    async def invoke(self, request):
        self.invoked.append(request)

    async def download_media(self, message, file_name, progress, progress_args):
        self.downloaded.append(message.id)
        if message.id in self.cancel_download_ids:
            raise asyncio.CancelledError()
        if message.id in self.fail_download_ids:
            raise OSError("Simulated download failure")
        kind = message.media.value
        media = getattr(message, kind)
        data = (kind + ":" + media.file_unique_id).encode("ascii")
        target = Path(file_name)
        target.write_bytes(data)
        await progress(len(data) // 2, len(data), *progress_args)
        await progress(len(data), len(data), *progress_args)
        return str(target)


async def check_download_pipeline(directory):
    from datetime import datetime, timezone

    from pyrogram.enums import ChatType, MessageMediaType

    from tg_media_archiver.media_downloader import Archiver
    from tg_media_archiver.module.app import Application, DownloadStatus, TaskNode

    app = Application("config.yaml")
    app.load_config()
    app.save_path = str(directory / "pipeline-downloads")
    app.temp_save_path = str(directory / "pipeline-temp")
    app.pre_run()
    archiver = Archiver(app)
    client = FakeTelegram()

    def message(message_id, kind, unique_id, album=None):
        data = (kind + ":" + unique_id).encode("ascii")
        media = SimpleNamespace(
            file_name=f"original-{message_id}.{'mp4' if kind == 'video' else 'jpg'}",
            file_unique_id=unique_id,
            file_size=len(data),
            mime_type="video/mp4" if kind == "video" else "image/jpeg",
        )
        item = SimpleNamespace(
            id=message_id,
            media=MessageMediaType(kind),
            from_user=SimpleNamespace(id=1),
            chat=SimpleNamespace(id=1, title=None, type=ChatType.PRIVATE),
            forward_date=datetime.now(timezone.utc),
            date=datetime.now(timezone.utc),
            media_group_id=album,
            caption=None,
        )
        setattr(item, kind, media)
        return item

    async def archive(item):
        node = TaskNode(
            chat_id=1,
            from_user_id=1,
            task_id=item.id,
            client=client,
            _persistent_queue=archiver.bot.persistent_queue,
        )
        assert await archiver.bot.persistent_queue.enqueue(1, item.id, 1)
        assert await archiver.bot.persistent_queue.mark_running(1, item.id)
        node._manual_started_ids.add(item.id)
        archiver.bot.manual_progress.track(node, item)
        await archiver.download_task(client, item, node)
        return node.download_status[item.id]

    first = message(40, "photo", "same-photo")
    assert await archive(first) is DownloadStatus.SuccessDownload
    first_path = await archiver.dedup.find_identity(
        archiver.dedup.identity("photo", "same-photo", first.photo.file_size)
    )
    assert first_path and Path(first_path).is_file()

    duplicate = message(41, "photo", "same-photo")
    assert await archive(duplicate) is DownloadStatus.SkipDownload
    assert client.downloaded == [40]

    Path(first_path).unlink()
    missing = message(42, "photo", "same-photo")
    assert await archive(missing) is DownloadStatus.SuccessDownload
    assert client.downloaded == [40, 42]

    mixed = [
        message(43, "photo", "album-photo", "mixed-1"),
        message(44, "video", "album-video", "mixed-1"),
    ]
    for item in mixed:
        assert await archive(item) is DownloadStatus.SuccessDownload
    assert client.downloaded == [40, 42, 43, 44]

    failed = message(45, "video", "missing-video")
    client.fail_download_ids.add(45)

    async def no_wait(_delay):
        return None

    with patch("tg_media_archiver.media_downloader.asyncio.sleep", new=no_wait):
        assert await archive(failed) is DownloadStatus.FailedDownload
    cancelled = message(46, "video", "cancelled-video")
    client.cancel_download_ids.add(46)
    failures_before_cancel = archiver.bot.manual_progress.counts["failed"]
    try:
        await archive(cancelled)
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("Cancelled transfer unexpectedly completed")
    pending_after_cancel = archiver.bot.manual_progress.pending[46][0]
    assert pending_after_cancel.download_status[46] is DownloadStatus.Downloading
    assert 46 not in pending_after_cancel._manual_started_ids
    archiver.bot.manual_progress.snapshot()
    assert 46 in archiver.bot.manual_progress.pending
    assert archiver.bot.manual_progress.counts["failed"] == failures_before_cancel

    worker_item = message(47, "video", "retry-after-db-lock")
    archiver.bot.client = client
    assert await archiver.bot._enqueue_message(worker_item)
    original_claim = archiver.bot.persistent_queue.mark_running
    claim_attempts = 0

    async def claim_after_lock(chat_id, message_id):
        nonlocal claim_attempts
        claim_attempts += 1
        if claim_attempts == 1:
            raise sqlite3.OperationalError("Simulated database lock")
        return await original_claim(chat_id, message_id)

    with (
        patch.object(
            archiver.bot.persistent_queue, "mark_running", new=claim_after_lock
        ),
        patch("tg_media_archiver.media_downloader.asyncio.sleep", new=no_wait),
    ):
        worker = asyncio.create_task(archiver.worker())
        try:
            await asyncio.wait_for(archiver.queue.join(), timeout=3)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    assert claim_attempts == 2
    assert client.downloaded[-1] == 47
    assert archiver.queue.empty() and 47 not in archiver.bot._scheduled_ids

    cleanup = await archiver.bot.persistent_queue.list_cleanup(1)
    assert {row["message_id"] for row in cleanup} == {40, 41, 42, 43, 44, 47}
    pending = await archiver.bot.persistent_queue.list_pending(recover_running=False)
    assert {row["message_id"] for row in pending} == {45, 46}
    assert {row["message_id"]: row["state"] for row in pending} == {
        45: "failed",
        46: "queued",
    }
    print("Forward download, SHA-256 skip, missing-file recovery and failures passed")


async def check_progress_and_effects(directory):
    import time

    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from tg_media_archiver.module.download_stat import get_download_result
    from tg_media_archiver.module.manual_progress import ManualProgress
    from tg_media_archiver.module.persistent_queue import PersistentQueue
    from tg_media_archiver.module.queue_effects import QueueEffects

    client = FakeTelegram()
    panel = ManualProgress(
        1,
        refresh_seconds=0.05,
        visible_seconds=0.18,
        settle_grace_seconds=0.05,
        state_path=directory / "panel.json",
        reposition_debounce=0.01,
        reposition_max_delay=0.05,
    )
    await panel.restore(client, InlineKeyboardMarkup, InlineKeyboardButton, 999)
    node = SimpleNamespace(
        task_id=1, chat_id=1, download_status={}, _manual_started_ids=set()
    )
    media = SimpleNamespace(value="video")
    forwarded = SimpleNamespace(
        id=10, media=media, video=SimpleNamespace(file_name="clip.mp4")
    )
    panel.track(node, forwarded)
    await panel.open(client, InlineKeyboardMarkup, InlineKeyboardButton)
    first_panel = panel.panel_id
    first_deadline = panel.visible_until
    assert first_panel in client.messages
    new_message = SimpleNamespace(
        id=first_panel + 1,
        chat=SimpleNamespace(id=1, type=SimpleNamespace(value="private")),
        from_user=SimpleNamespace(id=1),
    )
    client.next_id = new_message.id
    assert await panel.observe_message(
        client, new_message, InlineKeyboardMarkup, InlineKeyboardButton
    )
    for _ in range(50):
        if panel.panel_id != first_panel and first_panel in client.deleted:
            break
        await asyncio.sleep(0.02)
    assert panel.panel_id > new_message.id
    assert first_panel in client.deleted
    assert panel.visible_until == first_deadline

    get_download_result().setdefault(1, {})[10] = {"task_id": 1}
    node.download_status[10] = SimpleNamespace(name="SuccessDownload")
    panel.snapshot()
    assert panel.counts["success"] == 1 and not panel.pending
    assert 10 not in get_download_result().get(1, {})
    current_panel = panel.panel_id
    for _ in range(30):
        if (
            not panel.enabled
            and "✅ 当前任务已结束" in client.messages[current_panel].text
        ):
            break
        await asyncio.sleep(0.02)
    assert not panel.enabled
    assert "✅ 当前任务已结束" in client.messages[current_panel].text
    assert "处理进度 [" not in client.messages[current_panel].text
    assert any(
        key.callback_data == f"mp:open:{panel.generation}"
        for row in client.messages[current_panel].reply_markup.inline_keyboard
        for key in row
    )
    await panel.shutdown()
    assert current_panel in client.messages
    restored = ManualProgress(1, state_path=directory / "panel.json")
    assert await restored.restore(
        client, InlineKeyboardMarkup, InlineKeyboardButton, 999
    )
    assert restored.panel_id == current_panel
    await restored.open(client, InlineKeyboardMarkup, InlineKeyboardButton)
    assert restored.enabled
    assert "处理进度 [" in client.messages[current_panel].text
    restored_deadline = restored.visible_until
    await restored.shutdown()
    resumed = ManualProgress(
        1,
        refresh_seconds=0.02,
        visible_seconds=0.18,
        settle_grace_seconds=0.05,
        state_path=directory / "panel.json",
    )
    assert await resumed.restore(
        client, InlineKeyboardMarkup, InlineKeyboardButton, 999
    )
    assert resumed.visible_until == restored_deadline
    await asyncio.sleep(max(0, restored_deadline - time.time()) + 0.04)
    assert not resumed.enabled
    assert "处理进度 [" not in client.messages[current_panel].text
    await resumed.close(client)
    assert current_panel in client.deleted and resumed.panel_id is None
    await resumed.shutdown()

    ongoing = ManualProgress(
        1,
        refresh_seconds=0.02,
        visible_seconds=0.08,
        settle_grace_seconds=0.08,
    )
    ongoing_node = SimpleNamespace(
        task_id=2, chat_id=1, download_status={}, _manual_started_ids=set()
    )
    ongoing.track(ongoing_node, forwarded)
    next_forwarded = SimpleNamespace(
        id=11, media=media, video=SimpleNamespace(file_name="next.mp4")
    )
    second_node = SimpleNamespace(
        task_id=3, chat_id=1, download_status={}, _manual_started_ids=set()
    )
    ongoing.track(second_node, next_forwarded)
    await ongoing.open(client, InlineKeyboardMarkup, InlineKeyboardButton)
    ongoing_id = ongoing.panel_id
    for _ in range(30):
        if not ongoing.enabled and client.messages[ongoing_id].text == (
            "任务仍在进行中，如需查看进度条请点击按钮。"
        ):
            break
        await asyncio.sleep(0.02)
    assert not ongoing.enabled
    assert client.messages[ongoing_id].text == (
        "任务仍在进行中，如需查看进度条请点击按钮。"
    )

    class FakeQuery:
        def __init__(self, data, user_id):
            self.data = data
            self.from_user = SimpleNamespace(id=user_id)
            self.message = client.messages[ongoing_id]
            self.answers = []

        async def answer(self, text=None):
            self.answers.append(text)

    invalid = FakeQuery(f"mp:open:{ongoing.generation}", user_id=2)
    assert not await ongoing.callback(client, invalid)
    assert not ongoing.enabled
    reopen = FakeQuery(f"mp:open:{ongoing.generation}", user_id=1)
    assert await ongoing.callback(client, reopen)
    assert ongoing.enabled
    assert "处理进度 [" in client.messages[ongoing_id].text
    for _ in range(30):
        if not ongoing.enabled and client.messages[ongoing_id].text == (
            "任务仍在进行中，如需查看进度条请点击按钮。"
        ):
            break
        await asyncio.sleep(0.02)
    edits_while_parked = len(client.edits)
    ongoing_node.download_status[10] = SimpleNamespace(name="SuccessDownload")
    ongoing.snapshot()
    await asyncio.sleep(0.02)
    assert len(client.edits) == edits_while_parked
    second_node.download_status[11] = SimpleNamespace(name="SkipDownload")
    ongoing.snapshot()
    for _ in range(30):
        if "✅ 当前任务已结束" in client.messages[ongoing_id].text:
            break
        await asyncio.sleep(0.02)
    assert "成功 1 · 失败 0 · 跳过 1" in client.messages[ongoing_id].text
    assert len(client.edits) == edits_while_parked + 1
    close = FakeQuery(f"mp:off:{ongoing.generation}", user_id=1)
    assert await ongoing.callback(client, close)
    assert ongoing_id in client.deleted and ongoing.panel_id is None
    ongoing.track(
        SimpleNamespace(
            task_id=4, chat_id=1, download_status={}, _manual_started_ids=set()
        ),
        SimpleNamespace(
            id=12, media=media, video=SimpleNamespace(file_name="later.mp4")
        ),
    )
    assert ongoing.panel_id is None
    await ongoing.shutdown()

    share = directory / "effect-files"
    share.mkdir()
    queue = PersistentQueue(share / "files.db")
    assert await queue.enqueue(1, 20, 1)
    assert await queue.enqueue(1, 21, 1)
    assert await queue.mark_done(1, 20)
    assert await queue.mark_failed(1, 21, "DownloadFailed")
    effects = QueueEffects(queue, client, 1)
    original_next_completion = queue.next_completion

    async def due_completion(owner_id):
        return await original_next_completion(owner_id, quiet_seconds=0)

    with patch.object(queue, "next_completion", new=due_completion):
        await effects.tick()
    assert 20 in client.deleted and 21 not in client.deleted
    assert len(client.invoked) == 1
    assert "已下载 1 · 跳过 0 · 失败 1" in client.invoked[0].message
    assert await queue.mark_running(1, 21)
    assert await queue.mark_done(1, 21, "skipped")
    with patch.object(queue, "next_completion", new=due_completion):
        await effects.tick()
    assert 21 in client.deleted
    assert len(client.invoked) == 2
    assert "总计 1 · 已下载 0 · 跳过 1 · 失败 0" in client.invoked[1].message
    assert not await queue.list_cleanup(1)
    print("Manual progress, panel close/move and durable cleanup passed")


async def check_localized_chat(directory):
    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from tg_media_archiver.module.app import Application
    from tg_media_archiver.module.bot import DownloadBot
    from tg_media_archiver.module.manual_progress import ManualProgress
    from tg_media_archiver.module.messages import Messages
    from tg_media_archiver.module.persistent_queue import PersistentQueue

    english = Messages("en_US")
    chinese = Messages("zh_CN")
    client = FakeTelegram()
    app = Application("config.yaml")
    app.load_config()
    app.messages = english
    app.save_path = str(directory / "localized-downloads")
    app.log_file_path = str(directory / "localized-logs")
    bot = DownloadBot(app, lambda *_args: None)
    assert bot.manual_progress.messages is english
    assert bot.persistent_queue.messages is english
    await bot._on_help(client, SimpleNamespace(from_user=SimpleNamespace(id=1)))
    assert "Telegram Media Archiver" in client.messages[client.next_id].text

    state_path = directory / "localized-panel.json"
    panel = ManualProgress(
        1, messages=english, state_path=state_path, visible_seconds=60
    )
    assert not await panel.restore(
        client, InlineKeyboardMarkup, InlineKeyboardButton, 999
    )
    node = SimpleNamespace(
        task_id=1, chat_id=1, download_status={}, _manual_started_ids=set()
    )
    forwarded = SimpleNamespace(
        id=40,
        media=SimpleNamespace(value="video"),
        video=SimpleNamespace(file_name="clip.mp4"),
    )
    panel.track(node, forwarded)
    await panel.open(client, InlineKeyboardMarkup, InlineKeyboardButton)
    panel_id = panel.panel_id
    assert "Download progress" in client.messages[panel_id].text
    assert "Close progress" in str(client.messages[panel_id].reply_markup)
    await panel.shutdown()
    restored = ManualProgress(
        1, messages=chinese, state_path=state_path, visible_seconds=60
    )
    assert await restored.restore(
        client, InlineKeyboardMarkup, InlineKeyboardButton, 999
    )
    await restored.open(client, InlineKeyboardMarkup, InlineKeyboardButton)
    assert "下载进度" in client.messages[panel_id].text
    await restored.close(client)
    await restored.shutdown()

    queue_dir = directory / "localized-queue"
    queue_dir.mkdir()
    queue_path = queue_dir / "files.db"
    queue = PersistentQueue(queue_path, messages=english)
    assert await queue.enqueue(1, 50, 1)
    assert await queue.mark_done(1, 50, "downloaded")
    assert await queue.enqueue(1, 51, 1)
    assert await queue.mark_done(1, 51, "skipped")
    notice = await queue.next_completion(1, quiet_seconds=0)
    assert "Total 2 · Downloaded 1 · Skipped 1 · Failed 0" in notice["summary_text"]
    restarted = PersistentQueue(queue_path, messages=chinese)
    retry = await restarted.next_completion(1, quiet_seconds=0)
    assert retry["summary_text"] == notice["summary_text"]
    assert retry["notification_random_id"] == notice["notification_random_id"]
    assert await restarted.ack_completion(notice["id"])
    assert await restarted.enqueue(1, 52, 1)
    assert await restarted.mark_done(1, 52, "downloaded")
    chinese_notice = await restarted.next_completion(1, quiet_seconds=0)
    assert "总计 1 · 已下载 1 · 跳过 0 · 失败 0" in chinese_notice["summary_text"]
    print("Localized bot help, panel restore and frozen summaries passed")


def main():
    original_cwd = Path.cwd()
    with (
        tempfile.TemporaryDirectory(prefix="tg-archiver-validation-") as temporary,
        patch.dict(os.environ, {"TGA_LANGUAGE": "", "TGA_MESSAGES_DIR": ""}),
    ):
        try:
            os.chdir(temporary)
            check_installation()
            check_configuration()
            check_message_catalog(Path(temporary))
            asyncio.run(check_storage(Path(temporary)))
            asyncio.run(check_private_intake(Path(temporary)))
            asyncio.run(check_download_pipeline(Path(temporary)))
            asyncio.run(check_progress_and_effects(Path(temporary)))
            asyncio.run(check_localized_chat(Path(temporary)))
        finally:
            os.chdir(original_cwd)
    print("Offline validation passed; Telegram was not contacted")


if __name__ == "__main__":
    main()
