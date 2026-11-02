# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one redactor everything leaving the iLO 4 Redfish effect passes through (OMN-21835).

A result, a journal line, a ledger row, an evidence step and an exception description are all cleaned
here before they are stored or returned. It masks: values under keys named like a password, secret,
token, authorization, key, license or cookie; the ``X-Auth-Token`` and ``Authorization`` headers; and
the iLO license-key shape (five groups of five letters or digits).
"""

from __future__ import annotations

import re

from pydantic import JsonValue

MASK = "[REDACTED]"

_SENSITIVE_KEY = re.compile(
    r"(password|passwd|passphrase|secret|token|authorization|cookie|credential"
    r"|licen[sc]e[_-]?key|api[_-]?key|private[_-]?key)",
    re.IGNORECASE,
)
# A key named "Key" or ending in "Key" or "_key" holds key material.
_KEY_NAMED_KEY = re.compile(r"(^|[_-])key$|[a-z]Key$", re.IGNORECASE)
_LICENSE_KEY = re.compile(r"\b(?:[A-Z0-9]{5}-){4}[A-Z0-9]{5}\b")
_BEARER = re.compile(r"(?i)\b(x-auth-token|authorization)\s*[:=]\s*\S+")
_URL_USERINFO = re.compile(r"(?i)\b(https?://)[^/\s:@]+:[^/\s@]+@")

_MAX_TEXT = 2000


def _is_sensitive_key(name: str) -> bool:
    return bool(_SENSITIVE_KEY.search(name) or _KEY_NAMED_KEY.search(name))


def redact_text(text: str) -> str:
    """Mask license keys, auth headers and URL userinfo in free text, and bound its length."""
    cleaned = _LICENSE_KEY.sub(MASK, text)
    cleaned = _BEARER.sub(lambda m: f"{m.group(1)}: {MASK}", cleaned)
    cleaned = _URL_USERINFO.sub(lambda m: f"{m.group(1)}{MASK}@", cleaned)
    if len(cleaned) > _MAX_TEXT:
        cleaned = cleaned[:_MAX_TEXT] + "...[truncated]"
    return cleaned


def _redact_mapping(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        key: (
            MASK if _is_sensitive_key(key) and item is not None else redact_json(item)
        )
        for key, item in value.items()
    }


def redact_json(value: JsonValue) -> JsonValue:
    """Return ``value`` with sensitive keys masked and every string passed through :func:`redact_text`."""
    if isinstance(value, dict):
        return _redact_mapping(value)
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_object(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """:func:`redact_json` for a mapping, keeping the mapping type."""
    return _redact_mapping(value)


def pipe_free(text: str) -> str:
    """One ledger-safe line: no pipe, no newline."""
    return " ".join(text.replace("|", "/").split())
