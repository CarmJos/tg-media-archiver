"""Collect Telegram credentials through hidden terminal prompts."""

import getpass
import os
import re
from importlib.resources import files
from pathlib import Path

from ruamel.yaml import YAML


def configure() -> None:
    path = Path("config.yaml")
    yaml = YAML()
    template = (
        path
        if path.is_file()
        else files("tg_media_archiver").joinpath("config.example.yaml")
    )
    config = yaml.load(template.read_text(encoding="utf-8"))
    print("Telegram setup: input is hidden. Paste each value and press Enter.")
    api_id = getpass.getpass("API ID: ").strip()
    api_hash = getpass.getpass("API Hash: ").strip()
    bot_token = getpass.getpass("Bot Token: ").strip()
    if not api_id.isdigit() or int(api_id) <= 0:
        raise SystemExit("Invalid API ID. Nothing saved.")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        raise SystemExit("Invalid API Hash. Nothing saved.")
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", bot_token):
        raise SystemExit("Invalid Bot Token. Nothing saved.")
    config.update({"api_id": int(api_id), "api_hash": api_hash, "bot_token": bot_token})
    previous_umask = os.umask(0o077)
    try:
        with path.open("w", encoding="utf-8") as handle:
            yaml.dump(config, handle)
        path.chmod(0o600)
    finally:
        os.umask(previous_umask)
    print("Credentials saved to config.yaml. Configure the proxy before running login.")
