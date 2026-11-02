"""Authenticate both Telegram sessions and bind the bot to the signed-in user."""

import asyncio
import getpass
import logging
import os
from pathlib import Path

from pyrogram import Client
from pyrogram.errors import SessionPasswordNeeded
from ruamel.yaml import YAML


async def _login() -> None:
    path = Path("config.yaml")
    yaml = YAML()
    config = yaml.load(path.read_text(encoding="utf-8"))
    sessions = Path("sessions")
    sessions.mkdir(mode=0o700, exist_ok=True)
    common = dict(
        api_id=int(config["api_id"]),
        api_hash=config["api_hash"],
        proxy=config.get("proxy") or None,
        workdir=str(sessions),
        no_updates=True,
    )
    user = Client("media_downloader", **common)
    try:
        authorized = await user.connect()
        if not authorized:
            phone = getpass.getpass("Telegram phone number (+country code): ").strip()
            sent = await user.send_code(phone)
            print("Login code delivery:", sent.type)
            code = getpass.getpass("Telegram login code: ").replace(" ", "").strip()
            try:
                await user.sign_in(phone, sent.phone_code_hash, code)
            except SessionPasswordNeeded:
                password = getpass.getpass("Telegram two-step verification password: ")
                await user.check_password(password)
        me = await user.get_me()
        if me.is_bot:
            raise RuntimeError("Expected a personal Telegram account")
        config["allowed_user_ids"] = [me.id]
        print("Authorized user ID:", me.id)
    finally:
        if user.is_connected:
            await user.disconnect()

    bot = Client("media_downloader_bot", bot_token=config["bot_token"], **common)
    try:
        authorized = await bot.connect()
        if not authorized:
            await bot.sign_in_bot(config["bot_token"])
        identity = await bot.get_me()
        if not identity.is_bot or identity.id != int(config["bot_token"].split(":")[0]):
            raise RuntimeError("Bot session does not match the configured token")
        print("Bot authorized: @" + (identity.username or str(identity.id)))
    finally:
        if bot.is_connected:
            await bot.disconnect()

    with path.open("w", encoding="utf-8") as handle:
        yaml.dump(config, handle)
    path.chmod(0o600)
    (sessions / "initialized").write_text("ok\n", encoding="utf-8")
    print("Login complete. Run 'tg-media-archiver run' to start archiving.")


def login() -> int:
    logging.basicConfig(level=logging.ERROR)
    previous_umask = os.umask(0o077)
    try:
        asyncio.run(_login())
    except Exception as exc:
        print("Login failed:", type(exc).__name__)
        return 1
    finally:
        os.umask(previous_umask)
    return 0
