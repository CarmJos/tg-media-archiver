"""Offline smoke checks for an installed tg-media-archiver wheel."""

import asyncio
from importlib import import_module, metadata, resources
import os
from pathlib import Path
import pkgutil
import subprocess
import sys
import tempfile
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
    from tg_media_archiver.module.web import get_flask_app

    web = get_flask_app()
    for template in ("index.html", "login.html"):
        web.jinja_env.get_template(template)
    assert Path(web.static_folder, "layui", "layui.js").is_file()

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

    print(f"Installed wheel {metadata.version('tg-media-archiver')}: "
          f"{len(modules)} modules, CLI, resources and encryption passed")


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

    app = Application("config.yaml", "data.yaml", "offline-validation")
    try:
        app.load_config()
        assert app.web_enabled is False
        assert not app.chat_download_config
        assert app.max_concurrent_transmissions == 2
    finally:
        app.loop.close()
        app.executor.shutdown(wait=True)
    print("Hidden configuration and offline loading passed")


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
    print("File publishing, deduplication, queue recovery and cleanup passed")


def main():
    original_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="tg-archiver-validation-") as temporary:
        try:
            os.chdir(temporary)
            check_installation()
            check_configuration()
            asyncio.run(check_storage(Path(temporary)))
        finally:
            os.chdir(original_cwd)
            media = sys.modules.get("tg_media_archiver.media_downloader")
            if media is not None:
                media.app.loop.close()
                media.app.executor.shutdown(wait=True)
    print("Offline validation passed; Telegram was not contacted")


if __name__ == "__main__":
    main()
