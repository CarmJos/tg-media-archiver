"""Configure, authenticate, and run the archiver."""

import argparse
import os
from pathlib import Path

from tg_media_archiver import __version__


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="tg-media-archiver",
        description="Archive media forwarded to your private Telegram bot.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--workdir",
        type=Path,
        default=Path.cwd(),
        help="directory containing config.yaml and runtime state (default: current directory)",
    )
    parser.add_argument(
        "command",
        choices=("configure", "login", "run"),
        nargs="?",
        default="run",
    )
    args = parser.parse_args(argv)
    args.workdir.mkdir(parents=True, exist_ok=True)
    os.chdir(args.workdir)

    if args.command == "configure":
        from tg_media_archiver.configure import configure

        configure()
        return 0
    if not Path("config.yaml").is_file():
        parser.error("config.yaml is missing; run 'tg-media-archiver configure' first")
    if args.command == "login":
        from tg_media_archiver.login import login

        return login()
    if not Path("sessions/initialized").is_file():
        parser.error(
            "Telegram login is incomplete; run 'tg-media-archiver login' first"
        )

    os.umask(0o002)
    from tg_media_archiver import media_downloader

    if not media_downloader._check_config():
        return 1
    media_downloader.main()
    return 0
