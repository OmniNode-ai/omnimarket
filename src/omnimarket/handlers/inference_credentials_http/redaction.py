# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A 422 body that never echoes what the caller submitted.

A validation error normally carries the offending ``input``, which on a route
that accepts a provider key is the key. The renderer here keeps ``type``, ``loc``
and ``msg``, drops ``input`` and ``ctx``, restricts ``loc`` to the model's own
field names (a key pasted into the field-name position cannot ride out in the
location), and scrubs any value submitted under a secret-named or undeclared key
out of ``msg``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from pydantic import BaseModel, ValidationError

REDACTED: Final[str] = "***redacted***"
_MIN_SCRUBBED_LENGTH: Final[int] = 8
_LOC_CONTAINERS: Final[frozenset[str]] = frozenset(
    {"body", "query", "path", "header", "cookie"}
)
SECRET_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "credential",
        "credentials",
        "id_token",
        "key",
        "key_value",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "secret_key",
        "session_token",
        "token",
    }
)


def _strings_under(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, Mapping):
        found: set[str] = set()
        for key, item in value.items():
            if isinstance(key, str):
                found.add(key)
            found |= _strings_under(item)
        return found
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        found = set()
        for item in value:
            found |= _strings_under(item)
        return found
    return set()


def untrusted_strings(body: object, known: frozenset[str]) -> frozenset[str]:
    """Strings of the submitted body that must not reappear in a response."""
    if not isinstance(body, Mapping):
        return frozenset(_strings_under(body))
    untrusted: set[str] = set()
    for key, value in body.items():
        if not isinstance(key, str):
            continue
        unknown = key not in known
        if unknown or key in SECRET_FIELD_NAMES:
            if unknown:
                untrusted.add(key)
            untrusted |= _strings_under(value)
    return frozenset(s for s in untrusted if len(s) >= _MIN_SCRUBBED_LENGTH)


def _scrub(text: str, untrusted: Iterable[str]) -> str:
    for value in untrusted:
        if value in text:
            text = text.replace(value, REDACTED)
    return text


def render_validation_error(
    error: ValidationError, body: object, model: type[BaseModel]
) -> list[dict[str, Any]]:
    """The ``detail`` list of a secret-safe 422 for ``error``."""
    known = frozenset(model.model_fields)
    untrusted = untrusted_strings(body, known)
    rendered: list[dict[str, Any]] = []
    for item in error.errors(
        include_input=False, include_context=False, include_url=False
    ):
        loc: list[object] = ["body"]
        for segment in item.get("loc", ()):
            if isinstance(segment, str) and segment not in known:
                loc.append(REDACTED)
            else:
                loc.append(segment)
        rendered.append(
            {
                "type": item.get("type", ""),
                "loc": loc,
                "msg": _scrub(str(item.get("msg", "")), untrusted),
            }
        )
    return rendered


def unparseable_body_detail() -> list[dict[str, Any]]:
    """A 422 ``detail`` for a body that is not a JSON object; echoes nothing."""
    return [
        {
            "type": "json_invalid",
            "loc": ["body"],
            "msg": "Request body must be a JSON object",
        }
    ]
