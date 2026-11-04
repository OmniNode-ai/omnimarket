# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decode one work-ledger MSG, RELEASE or HOLD record into a spool entry.

A lane reads its inbox once at launch, so a RELEASE or HOLD appended while it
runs reaches it only through the spool, beside the MSG rows. The wire record carries the exact ledger row as ``raw_row`` and its sha256 as
``row_id``; the row is the truth, so the addressing cells are re-read from it
and a record whose hash does not match its row is refused as malformed.
"""

from __future__ import annotations

import json
import re

from omnimarket.events.model_ledger_row_event import work_ledger_row_id

from ..models import ModelAgentInboxSpoolEntry
from ..models.model_agent_inbox_spool_entry import SpoolRowType

_CELL = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)=(.*)$", re.DOTALL)
_SEPARATOR = " | "
BROADCAST_RECIPIENT = "all"
# MSG must name a recipient; a RELEASE or HOLD with no to= addresses no lane.
SPOOLED_ROW_TYPES: dict[str, SpoolRowType] = {
    "MSG": "MSG",
    "RELEASE": "RELEASE",
    "HOLD": "HOLD",
}
ADDRESS_REQUIRED_ROW_TYPES = ("MSG",)


class MalformedLedgerMsgError(ValueError):
    """The record is not a well-formed MSG, RELEASE or HOLD row event."""


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
    row_type = SPOOLED_ROW_TYPES.get(parts[1]) if len(parts) >= 3 else None
    if row_type is None:
        raise MalformedLedgerMsgError("row is not a MSG, RELEASE or HOLD row")
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
    if not recipients and row_type in ADDRESS_REQUIRED_ROW_TYPES:
        raise MalformedLedgerMsgError(f"{row_type} row has no to= cell")
    return ModelAgentInboxSpoolEntry(
        row_id=row_id,
        row_type=row_type,
        msg_id=cells.get("id", ""),
        # A MSG names its sender in from=; a RELEASE or HOLD in lane=.
        sender=cells.get("from" if row_type == "MSG" else "lane", ""),
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
    """The host's lanes this row is addressed to; ``all`` reaches every lane."""
    if BROADCAST_RECIPIENT in entry.recipients:
        return lanes
    return tuple(lane for lane in lanes if lane in entry.recipients)
