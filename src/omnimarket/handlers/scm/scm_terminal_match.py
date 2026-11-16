# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pick the payload out of a terminal event and say whether it names a command."""

from __future__ import annotations

import uuid

from omnimarket.lab_work.bus import _uuid_or_none


def terminal_payload(
    raw: dict[str, object], envelope_id: uuid.UUID, correlation_id: uuid.UUID
) -> dict[str, object] | None:
    """The payload of ``raw`` when it names the command, else ``None``.

    An event published with no envelope (the landing tick orchestrator's answers)
    is its own payload.
    """
    payload = raw.get("payload")
    if isinstance(payload, dict):
        body = payload
    elif "payload" not in raw and "envelope_id" not in raw:
        body = raw
    else:
        body = {}
    names_it = (
        _uuid_or_none(raw.get("parent_envelope_id")) == envelope_id
        or _uuid_or_none(raw.get("correlation_id")) == correlation_id
        or _uuid_or_none(body.get("correlation_id")) == correlation_id
    )
    return body if names_it else None
