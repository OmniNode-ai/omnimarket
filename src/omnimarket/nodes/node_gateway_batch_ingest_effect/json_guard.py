# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bounded JSON reading: nesting is measured in one linear pass before any parse, duplicate member names are kept visible."""

from __future__ import annotations

import json


class JsonRefusedError(Exception):
    """The body is not a JSON document the door will read; `reason` is the named reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TrackedDict(dict[str, object]):
    """A JSON object that remembers the member names it was given more than once."""

    duplicates: tuple[str, ...] = ()


def max_nesting(body: bytes) -> int:
    """The deepest [ / { nesting of a JSON text, measured outside strings, without parsing it."""
    depth = deepest = 0
    in_string = escaped = False
    for byte in body:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):
            depth += 1
            deepest = max(deepest, depth)
        elif byte in (0x5D, 0x7D):
            depth -= 1
    return deepest


def _pairs(pairs: list[tuple[str, object]]) -> TrackedDict:
    result = TrackedDict()
    seen: set[str] = set()
    duplicated: list[str] = []
    for name, value in pairs:
        if name in seen:
            duplicated.append(name)
        seen.add(name)
        result[name] = value
    result.duplicates = tuple(duplicated)
    return result


def parse_json(body: bytes, *, max_depth: int) -> object:
    """Parse `body`; refuse nesting past `max_depth` before parsing and anything that is not UTF-8 JSON."""
    if max_nesting(body) > max_depth:
        raise JsonRefusedError("json-too-deep")
    try:
        return json.loads(body.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise JsonRefusedError("body-not-json") from None


__all__ = ["JsonRefusedError", "TrackedDict", "max_nesting", "parse_json"]
