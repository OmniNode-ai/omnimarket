# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Size bound of a dead-letter envelope's original message (OMN-19448).

The projection DLQ routing (``omnimarket.projection.dlq``) re-exports these names.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Final

# OMN-19448: the DLQ producer refuses a request over 1 MiB, and a poison event
# it cannot publish is never acknowledged, so the partition behind it stalls.
# Keep the published envelope under this size; the reserve covers the envelope's
# own keys and the capped failure reason.
DLQ_MAX_ENVELOPE_BYTES: Final[int] = 900_000
_ENVELOPE_RESERVE_BYTES: Final[int] = 32_768
_MAX_FAILURE_REASON_CHARS: Final[int] = 4096
_MAX_KEPT_STRING_CHARS: Final[int] = 2048
_KEPT_PREFIX_CHARS: Final[int] = 512
_MAX_DROPPED_KEYS: Final[int] = 100
_IDENTITY_KEYS: Final[tuple[str, ...]] = (
    "correlation_id",
    "session_id",
    "tenant_id",
    "task_type",
    "status",
)


def _shorten_dlq_strings(value: Any) -> Any:
    """Return a copy of a JSON tree with every long string cut to a prefix.

    The cut string ends with its original length and sha256, so the evidence
    stays checkable against the source event on the bus.
    """
    if isinstance(value, str) and len(value) > _MAX_KEPT_STRING_CHARS:
        digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
        omitted = len(value) - _KEPT_PREFIX_CHARS
        return (
            value[:_KEPT_PREFIX_CHARS]
            + f"...[truncated {omitted} chars, sha256={digest}]"
        )
    if isinstance(value, dict):
        return {key: _shorten_dlq_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_shorten_dlq_strings(item) for item in value]
    return value


def bound_dlq_original_message(
    message: dict[str, Any], max_bytes: int = DLQ_MAX_ENVELOPE_BYTES
) -> tuple[dict[str, Any], bool]:
    """Fit a poison event into ``max_bytes`` without losing its identity.

    A message that fits is returned as the same object. Otherwise long strings
    are cut to a prefix with their digest; if the tree is still too large, only
    its scalar identity keys, the names of the dropped keys, its size and the
    sha256 of its full JSON are kept. The boolean says whether anything was cut.
    """
    raw = json.dumps(message, default=str).encode("utf-8")
    if len(raw) <= max_bytes:
        return message, False
    reduced = _shorten_dlq_strings(message)
    if len(json.dumps(reduced, default=str).encode("utf-8")) <= max_bytes:
        return reduced, True
    dropped = [str(key) for key in message if key not in _IDENTITY_KEYS]
    minimal: dict[str, Any] = {
        key: _shorten_dlq_strings(message[key])
        for key in _IDENTITY_KEYS
        if isinstance(message.get(key), str | int | float | bool)
    }
    minimal["dropped_keys"] = dropped[:_MAX_DROPPED_KEYS]
    minimal["dropped_key_count"] = len(dropped)
    minimal["original_bytes"] = len(raw)
    minimal["sha256"] = hashlib.sha256(raw).hexdigest()
    return minimal, True
