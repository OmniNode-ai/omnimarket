# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Snapshot wire-value serialization shared by projection writers and readers."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from uuid import UUID


def snapshot_json_value(value: Any, *, decode_json_string: bool = False) -> Any:
    """Serialize one row value for a snapshot delta / HTTP response.

    Shared by the writer path (:mod:`omnimarket.projection.runner`) and the
    serving path (:mod:`omnimarket.projection.api_server`) so both produce and
    consume the identical JSON shape for the same column value.
    """
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if decode_json_string and isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value
