# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where one setting sits in an overlay document, and the refresh that keeps an operator's value (OMN-26495).

A setting is one value of a document, named by a path: its keys from the document's root, joined by ``/``. A list
whose items are all mappings with a distinct string ``name`` is addressed by that name, ``hosts/[name=h1]/cap``, so
a host the repository adds ahead of it moves nothing; any other list is addressed by position, ``roots/[0]``. A key
is escaped (``~`` as ``~0``, ``/`` as ``~1``, a leading ``[`` as ``~2``), so a path always splits on ``/``.

A record names the paths an operator set through the store (``operator_paths``). A refresh takes the repository's
document and puts the live value back at each of those paths: every other setting takes the repository's value, the
way the whole document did before. A value is never printed: a report names a path and carries a digest.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import JsonValue

_NAME_PREFIX: Final = "[name="


class SettingPathError(ValueError):
    """A path names a place the document has no room for."""


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"


MISSING: Final = _Missing()


def value_digest(value: JsonValue) -> str:
    """The digest a report carries in place of a value: sha256 of its canonical JSON."""
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def escape_key(key: str) -> str:
    escaped = key.replace("~", "~0").replace("/", "~1")
    return "~2" + escaped[1:] if escaped.startswith("[") else escaped


def _unescape_key(segment: str) -> str:
    text = "[" + segment[2:] if segment.startswith("~2") else segment
    return text.replace("~1", "/").replace("~0", "~")


def named_item(name: str) -> str:
    return f"{_NAME_PREFIX}{escape_key(name)}]"


def _name_of(segment: str) -> str | None:
    if segment.startswith(_NAME_PREFIX) and segment.endswith("]"):
        return _unescape_key(segment[len(_NAME_PREFIX) : -1])
    return None


def _index_of(segment: str) -> int | None:
    inner = segment[1:-1] if segment.startswith("[") and segment.endswith("]") else ""
    return int(inner) if inner.isdigit() else None


def _find(items: Sequence[JsonValue], segment: str) -> int | None:
    """The position of the item a list segment names, or None when the list holds none."""
    name = _name_of(segment)
    if name is not None:
        for position, item in enumerate(items):
            if isinstance(item, dict) and item.get("name") == name:
                return position
        return None
    index = _index_of(segment)
    if index is None:
        raise SettingPathError(f"{segment!r} does not address a list item")
    return index if index < len(items) else None


def parse_path(path: str) -> list[str]:
    if not path or any(not part for part in path.split("/")):
        raise SettingPathError("a setting path is one or more non-empty segments")
    return path.split("/")


def get_at(document: JsonValue, path: str) -> JsonValue | _Missing:
    """The value at ``path``, or MISSING when the document holds none there."""
    node: JsonValue | _Missing = document
    for segment in parse_path(path):
        if isinstance(node, dict):
            node = node.get(_unescape_key(segment), MISSING)
        elif isinstance(node, list):
            position = _find(node, segment)
            node = MISSING if position is None else node[position]
        else:
            return MISSING
        if isinstance(node, _Missing):
            return MISSING
    return node


def set_at(document: dict[str, JsonValue], path: str, value: JsonValue) -> None:
    """Put ``value`` at ``path`` of ``document``, creating the mappings and named list items on the way."""
    segments = parse_path(path)
    node: JsonValue = document
    for depth, segment in enumerate(segments):
        last = depth == len(segments) - 1
        if isinstance(node, dict):
            key = _unescape_key(segment)
            if last:
                node[key] = value
                return
            child = node.get(key)
            if child is None:
                child = {} if not segments[depth + 1].startswith("[") else []
                node[key] = child
            node = child
        elif isinstance(node, list):
            position = _find(node, segment)
            if position is None:
                name = _name_of(segment)
                if name is None:
                    raise SettingPathError(f"{path}: the list has no item {segment}")
                node.append({"name": name})
                position = len(node) - 1
            if last:
                node[position] = value
                return
            node = node[position]
        else:
            raise SettingPathError(
                f"{path}: {segment} sits under a value that is not a container"
            )


@dataclass(frozen=True)
class Drift:
    """One operator-set setting whose live value differs from the repository's: digests only."""

    path: str
    repository_digest: str | None
    live_digest: str


@dataclass(frozen=True)
class Refresh:
    document: dict[str, JsonValue]
    operator_paths: tuple[str, ...]
    drift: tuple[Drift, ...]


def refresh_document(
    repository: Mapping[str, JsonValue],
    live: Mapping[str, JsonValue],
    operator_paths: Sequence[str],
) -> Refresh:
    """The repository's document with each operator-set live value put back, and the differences found.

    An operator path the live document holds no value at is dropped (nothing to keep). A path the repository no
    longer holds keeps its live value until an operator clears it, and is reported as drift with no repository digest.
    """
    merged: dict[str, JsonValue] = copy.deepcopy(dict(repository))
    kept: list[str] = []
    drift: list[Drift] = []
    for path in sorted(set(operator_paths)):
        held = get_at(dict(live), path)
        if isinstance(held, _Missing):
            continue
        wanted = get_at(dict(repository), path)
        set_at(merged, path, copy.deepcopy(held))
        kept.append(path)
        live_digest = value_digest(held)
        if isinstance(wanted, _Missing):
            drift.append(Drift(path, None, live_digest))
        elif value_digest(wanted) != live_digest:
            drift.append(Drift(path, value_digest(wanted), live_digest))
    return Refresh(merged, tuple(kept), tuple(drift))


__all__ = [
    "MISSING",
    "Drift",
    "Refresh",
    "SettingPathError",
    "escape_key",
    "get_at",
    "named_item",
    "parse_path",
    "refresh_document",
    "set_at",
    "value_digest",
]
