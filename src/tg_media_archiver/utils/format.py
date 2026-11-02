"""Portable archive path components."""

import os
import re
import unicodedata


def validate_title(title):
    value = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", str(title)).strip().rstrip(".")
    return value if value not in ("", ".", "..") else "_"


def truncate_filename(path, limit=230):
    directory, name = os.path.split(path)
    stem, suffix = os.path.splitext(unicodedata.normalize("NFC", name))
    suffix = suffix.encode("utf-8")[:32].decode("utf-8", errors="ignore")
    stem = stem.encode("utf-8")[: limit - len(suffix.encode("utf-8"))].decode(
        "utf-8", errors="ignore"
    )
    return os.path.join(directory, stem + suffix)
