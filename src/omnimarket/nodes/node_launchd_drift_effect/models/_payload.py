# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""JSON boundary helpers shared by frozen host-drift dataclasses."""

from __future__ import annotations

import types
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, cast, get_args, get_origin, get_type_hints


def iso_z(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must have a timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso_z(value)
    if isinstance(value, (Path, StrEnum)):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: encode(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    return value


def encode_model(value: Any) -> dict[str, object]:
    return cast(dict[str, object], encode(value))


def _decode(hint: Any, value: Any, *, ignore_unknown: bool = False) -> Any:
    origin, args = get_origin(hint), get_args(hint)
    if origin is types.UnionType:
        for option in args:
            try:
                return _decode(option, value, ignore_unknown=ignore_unknown)
            except (ValueError, TypeError):
                pass
        raise ValueError(f"invalid value for {hint}")
    if hint is type(None):
        if value is not None:
            raise ValueError("expected null")
        return None
    if hint is datetime:
        if not isinstance(value, str):
            raise ValueError("expected ISO timestamp")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamp must have a timezone")
        return parsed
    if hint is Path:
        if not isinstance(value, str):
            raise ValueError("expected path string")
        return Path(value)
    if origin is tuple:
        if not isinstance(value, (tuple, list)):
            raise ValueError("expected list")
        if args[-1] is Ellipsis:
            return tuple(
                _decode(args[0], v, ignore_unknown=ignore_unknown) for v in value
            )
        if len(args) != len(value):
            raise ValueError("invalid tuple length")
        return tuple(
            _decode(t, v, ignore_unknown=ignore_unknown)
            for t, v in zip(args, value, strict=True)
        )
    if origin is dict:
        if not isinstance(value, dict):
            raise ValueError("expected mapping")
        return {
            _decode(args[0], k, ignore_unknown=ignore_unknown): _decode(
                args[1], v, ignore_unknown=ignore_unknown
            )
            for k, v in value.items()
        }
    if hint is object:
        return value
    if isinstance(hint, type) and is_dataclass(hint):
        return decode_model(hint, value, ignore_unknown=ignore_unknown)
    if isinstance(hint, type) and issubclass(hint, StrEnum):
        return hint(value)
    if type(value) is not hint:
        raise ValueError(f"expected {hint}, got {type(value).__name__}")
    return value


def decode_model[T](
    cls: type[T], payload: dict[str, object], *, ignore_unknown: bool = False
) -> T:
    if not isinstance(payload, dict):
        raise ValueError("expected model mapping")
    hints = get_type_hints(cls)
    if not ignore_unknown and payload.keys() - hints.keys():
        raise ValueError("unknown model fields")
    try:
        return cls(
            **{
                k: _decode(hints[k], v, ignore_unknown=ignore_unknown)
                for k, v in payload.items()
                if k in hints
            }
        )
    except (TypeError, KeyError) as exc:
        raise ValueError(f"malformed {cls.__name__}: {exc}") from exc
