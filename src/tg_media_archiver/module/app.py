"""Configuration and in-memory state for the private media archiver."""

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ruamel.yaml import YAML

from tg_media_archiver.module.messages import Messages
from tg_media_archiver.utils.format import validate_title as safe_component


class DownloadStatus(Enum):
    Downloading = 1
    SuccessDownload = 2
    FailedDownload = 3
    SkipDownload = 4


@dataclass
class TaskNode:
    chat_id: int
    from_user_id: int
    task_id: int
    client: object = None
    download_status: dict = field(default_factory=dict)
    _manual_started_ids: set = field(default_factory=set)
    _persistent_queue: object = None


_MEDIA_TYPES = frozenset({"photo", "video", "document"})
_PATH_PREFIXES = frozenset({"chat_title", "media_datetime", "media_type"})
_NAME_PREFIXES = frozenset({"message_id", "file_name", "caption"})


class Application:
    """Load the settings needed for private forwarded-media downloads."""

    def __init__(self, config_file: str = "config.yaml"):
        self.config_file = Path(config_file)
        self.application_name = "media_downloader"
        self.save_path = str(Path("downloads").resolve())
        self.temp_save_path = str(Path("temp").resolve())
        self.session_file_path = str(Path("sessions").resolve())
        self.log_file_path = str(Path("log").resolve())
        self.api_id = 0
        self.api_hash = ""
        self.bot_token = ""
        self.proxy = None
        self.allowed_user_ids: list[int] = []
        self.media_types = ["photo", "video", "document"]
        self.file_formats = {"video": ["all"], "document": ["all"]}
        self.file_path_prefix = ["media_datetime"]
        self.file_name_prefix = ["message_id", "file_name"]
        self.file_name_prefix_split = "_"
        self.date_format = "%Y_%m"
        self.max_download_task = 3
        self.max_concurrent_transmissions = 2
        self.start_timeout = 120
        self.log_level = "INFO"
        self.messages = Messages()

    def load_config(self) -> None:
        config = YAML(typ="safe").load(self.config_file.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise TypeError("config.yaml must contain a mapping")

        language = os.environ.get("TGA_LANGUAGE") or config.get("language", "zh_CN")
        directory = os.environ.get("TGA_MESSAGES_DIR") or config.get(
            "messages_dir", "messages"
        )
        if not isinstance(directory, str) or not directory.strip():
            raise ValueError("messages_dir must be a nonempty path")
        message_path = Path(directory).expanduser()
        if not message_path.is_absolute():
            message_path = self.config_file.resolve().parent / message_path
        self.messages = Messages(language, message_path)

        self.api_id = int(config["api_id"])
        self.api_hash = str(config["api_hash"])
        self.bot_token = str(config["bot_token"])
        if self.api_id <= 0 or not self.api_hash or not self.bot_token:
            raise ValueError("api_id, api_hash, and bot_token are required")

        raw_owner_ids = config.get("allowed_user_ids", [])
        if (
            not isinstance(raw_owner_ids, list)
            or len(raw_owner_ids) != 1
            or isinstance(raw_owner_ids[0], bool)
            or not str(raw_owner_ids[0]).isdigit()
            or int(raw_owner_ids[0]) <= 0
        ):
            raise ValueError(
                "allowed_user_ids must contain exactly one positive Telegram ID"
            )
        self.allowed_user_ids = [int(raw_owner_ids[0])]

        root = Path(config.get("save_path") or "downloads").expanduser()
        self.save_path = str(root.resolve())
        self.proxy = config.get("proxy") or None
        if self.proxy is not None and not isinstance(self.proxy, dict):
            raise ValueError("proxy must be a mapping")

        media = config.get("media_types", self.media_types)
        if (
            not isinstance(media, list)
            or not media
            or any(item not in _MEDIA_TYPES for item in media)
        ):
            raise ValueError("media_types must contain photo, video, or document")
        self.media_types = list(dict.fromkeys(media))

        formats = config.get("file_formats", self.file_formats)
        if not isinstance(formats, dict):
            raise TypeError("file_formats must be a mapping")
        self.file_formats = {}
        for kind in self.media_types:
            values = formats.get(kind, ["all"])
            if (
                not isinstance(values, list)
                or not values
                or any(not isinstance(value, str) or not value for value in values)
            ):
                raise ValueError(
                    "file_formats entries must be nonempty lists of extensions"
                )
            self.file_formats[kind] = [value.lower().lstrip(".") for value in values]

        path_prefix = config.get("file_path_prefix", self.file_path_prefix)
        name_prefix = config.get("file_name_prefix", self.file_name_prefix)
        if not isinstance(path_prefix, list) or any(
            item not in _PATH_PREFIXES for item in path_prefix
        ):
            raise ValueError("file_path_prefix contains an unsupported component")
        if not isinstance(name_prefix, list) or any(
            item not in _NAME_PREFIXES for item in name_prefix
        ):
            raise ValueError("file_name_prefix contains an unsupported component")
        self.file_path_prefix = path_prefix
        self.file_name_prefix = name_prefix
        self.file_name_prefix_split = str(
            config.get("file_name_prefix_split", self.file_name_prefix_split)
        )
        self.date_format = str(config.get("date_format", self.date_format))

        for key in (
            "max_download_task",
            "max_concurrent_transmissions",
            "start_timeout",
        ):
            value = int(config.get(key, getattr(self, key)))
            if value <= 0:
                raise ValueError(f"{key} must be positive")
            setattr(self, key, value)
        self.log_level = str(config.get("log_level", self.log_level)).upper()

    def pre_run(self) -> None:
        Path(self.save_path).mkdir(parents=True, exist_ok=True, mode=0o775)
        for path in (self.temp_save_path, self.session_file_path, self.log_file_path):
            Path(path).mkdir(parents=True, exist_ok=True, mode=0o700)

    def get_file_save_path(
        self, media_type: str, chat_title: str, media_datetime: str
    ) -> str:
        parts = {
            "media_type": media_type,
            "chat_title": chat_title,
            "media_datetime": media_datetime,
        }
        path = Path(self.save_path)
        for prefix in self.file_path_prefix:
            path /= safe_component(parts[prefix])
        return str(path)

    def get_file_name(
        self, message_id: int, file_name: str | None, caption: str | None
    ) -> str:
        parts = {
            "message_id": str(message_id),
            "file_name": file_name,
            "caption": caption,
        }
        selected = [
            safe_component(parts[prefix])
            for prefix in self.file_name_prefix
            if parts[prefix]
        ]
        return safe_component(self.file_name_prefix_split.join(selected) or message_id)
