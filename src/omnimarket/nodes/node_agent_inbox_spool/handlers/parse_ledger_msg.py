# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decode one work-ledger MSG record into a spool entry.

The wire record carries the exact ledger row as ``raw_row`` and its sha256 as
``row_id``; the row is the truth, so the addressing cells are re-read from it
and a record whose hash does not match its row is refused as malformed.
"""

from __future__ import annotations

import json
import re

from omnimarket.events.model_ledger_row_event import work_ledger_row_id

from ..models import ModelAgentInboxSpoolEntry

_CELL = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)=(.*)$", re.DOTALL)
_SEPARATOR = " | "
BROADCAST_RECIPIENT = "all"


class MalformedLedgerMsgError(ValueError):
    """The record is not a well-formed MSG row event."""


def decode_msg_record(value: bytes, consumer_group: str) -> ModelAgentInboxSpoolEntry:
    try:
        decoded = json.loads(value)
    except ValueError as exc:
        raise MalformedLedgerMsgError("record is not JSON") from exc
    if not isinstance(decoded, dict):
        raise MalformedLedgerMsgError("record is not an object")
    payload = decoded.get("payload", decoded)
    if not isinstance(payload, dict):
        raise MalformedLedgerMsgError("payload is not an object")
    raw_row, row_id = payload.get("raw_row"), payload.get("row_id")
    if not isinstance(raw_row, str) or not isinstance(row_id, str):
        raise MalformedLedgerMsgError("record lacks raw_row or row_id")
    if work_ledger_row_id(raw_row) != row_id:
        raise MalformedLedgerMsgError("row_id does not match raw_row")
    parts = raw_row.strip().split(_SEPARATOR)
    if len(parts) < 3 or parts[1] != "MSG":
        raise MalformedLedgerMsgError("row is not a MSG row")
    cells: dict[str, str] = {}
    index = 2
    while index < len(parts):
        match = _CELL.match(parts[index])
        if match is None:
            break
        cells.setdefault(match.group(1), match.group(2))
        index += 1
    recipients = tuple(
        item.strip() for item in cells.get("to", "").split(",") if item.strip()
    )
    if not recipients:
        raise MalformedLedgerMsgError("MSG row has no to= cell")
    return ModelAgentInboxSpoolEntry(
        row_id=row_id,
        msg_id=cells.get("id", ""),
        sender=cells.get("from", ""),
        recipients=recipients,
        ticket=cells.get("ticket", ""),
        row_timestamp=parts[0].strip(),
        text=_SEPARATOR.join(parts[index:]),
        raw_row=raw_row,
        consumer_group=consumer_group,
    )


def addressed_lanes(
    entry: ModelAgentInboxSpoolEntry, lanes: tuple[str, ...]
) -> tuple[str, ...]:
    """The host's lanes this MSG is addressed to; ``all`` reaches every lane."""
    if BROADCAST_RECIPIENT in entry.recipients:
        return lanes
    return tuple(lane for lane in lanes if lane in entry.recipients)
