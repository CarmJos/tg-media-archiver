"""Load the bundled messages and optional per-language overrides."""

import re
from importlib import resources
from pathlib import Path
from string import Formatter
from types import MappingProxyType

from ruamel.yaml import YAML

_LANGUAGE = re.compile(r"[a-z]{2,3}_[A-Z]{2}\Z")
_FIELD = re.compile(r"[a-z][a-z0-9_]*\Z")
_FORMATTER = Formatter()
_BUILTIN_LANGUAGES = frozenset({"zh_CN", "en_US"})


def _flatten(mapping, source, prefix="", ancestors=None):
    if not isinstance(mapping, dict):
        raise ValueError(f"{source} must contain a YAML mapping")
    ancestors = set() if ancestors is None else ancestors
    identity = id(mapping)
    if identity in ancestors:
        raise ValueError(f"{source} has a recursive message group: {prefix}")
    ancestors.add(identity)
    result = {}
    try:
        for name, value in mapping.items():
            if not isinstance(name, str) or not _FIELD.fullmatch(name):
                raise ValueError(f"{source} has an invalid message key: {name!r}")
            key = f"{prefix}.{name}" if prefix else name
            if isinstance(value, dict):
                if not value:
                    raise ValueError(f"{source} has an empty message group: {key}")
                result.update(_flatten(value, source, key, ancestors))
            elif isinstance(value, str):
                result[key] = value
            else:
                raise ValueError(f"{source} message {key} must be a string")
    finally:
        ancestors.remove(identity)
    return result


def _read_catalog(stream, source):
    try:
        data = YAML(typ="safe").load(stream)
    except Exception as exc:
        raise ValueError(f"Cannot parse message catalog {source}: {exc}") from exc
    return _flatten(data, source)


def _builtins(language):
    source = resources.files("tg_media_archiver.messages").joinpath(f"{language}.yml")
    with source.open("r", encoding="utf-8") as stream:
        return _read_catalog(stream, source)


def _fields(template, source, key):
    fields = set()
    try:
        parts = _FORMATTER.parse(template)
        for _literal, field, spec, conversion in parts:
            if field is None:
                continue
            if not _FIELD.fullmatch(field) or spec or conversion:
                raise ValueError(
                    f"{source} message {key} has an unsupported placeholder: "
                    f"{{{field}}}"
                )
            fields.add(field)
    except ValueError as exc:
        if f"{source} message {key}" in str(exc):
            raise
        raise ValueError(f"{source} message {key} has invalid braces: {exc}") from exc
    return fields


class Messages:
    """Read-only, validated messages for one Telegram UI language."""

    __slots__ = ("language", "directory", "_catalog")

    def __init__(self, language="zh_CN", directory=None):
        if not isinstance(language, str) or not _LANGUAGE.fullmatch(language):
            raise ValueError("language must be a locale such as zh_CN or en_US")
        directory = Path(directory).expanduser().resolve() if directory else None
        if directory is not None and directory.exists() and not directory.is_dir():
            raise ValueError(f"messages_dir is not a directory: {directory}")

        english = _builtins("en_US")
        chinese = _builtins("zh_CN")
        if english.keys() != chinese.keys():
            raise ValueError("Bundled message catalogs have different keys")
        expected = {key: _fields(value, "en_US", key) for key, value in english.items()}
        for key, value in chinese.items():
            if _fields(value, "zh_CN", key) != expected[key]:
                raise ValueError(f"Bundled catalogs disagree on placeholders: {key}")

        catalog = (chinese if language == "zh_CN" else english).copy()
        override = directory / f"{language}.yml" if directory else None
        if override is not None and override.exists() and not override.is_file():
            raise ValueError(f"Message catalog is not a regular file: {override}")
        if language not in _BUILTIN_LANGUAGES and (
            override is None or not override.is_file()
        ):
            raise ValueError(
                f"No messages catalog for {language}; expected {override or f'{language}.yml'}"
            )
        if override is not None and override.is_file():
            with override.open("r", encoding="utf-8") as stream:
                custom = _read_catalog(stream, override)
            for key, value in custom.items():
                if key not in expected:
                    raise ValueError(f"{override} has an unknown message key: {key}")
                unknown = _fields(value, override, key) - expected[key]
                if unknown:
                    raise ValueError(
                        f"{override} message {key} has unknown placeholders: "
                        f"{', '.join(sorted(unknown))}"
                    )
            catalog.update(custom)
        self.language = language
        self.directory = directory
        self._catalog = MappingProxyType(catalog)

    def text(self, key, **values):
        """Format a dotted message key with its named placeholders."""
        try:
            template = self._catalog[key]
        except KeyError as exc:
            raise KeyError(f"Unknown message key: {key}") from exc
        return template.format(**values)
