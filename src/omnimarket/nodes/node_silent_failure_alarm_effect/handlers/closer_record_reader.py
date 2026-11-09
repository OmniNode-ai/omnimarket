# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the closer's declared interval and its newest run record (OMN-20718).

Reads only; the decision is HandlerCloserStalenessAlarm. A source that cannot be read is
reported as unreadable, never as an empty history.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.handlers.ledger_row_grammar import parse_row

STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _parse(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, STAMP_FORMAT).replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def declared_interval(overlay: Path, process_id: str) -> int | None:
    """The ``expected_interval_seconds`` the liveness overlay declares for ``process_id``."""
    try:
        document = yaml.safe_load(overlay.read_text())
    except (OSError, yaml.YAMLError):
        return None
    processes = document.get("processes") if isinstance(document, dict) else None
    for entry in processes if isinstance(processes, list) else ():
        if isinstance(entry, dict) and entry.get("process_id") == process_id:
            value = entry.get("expected_interval_seconds")
            return value if isinstance(value, int) and value > 0 else None
    return None


def ledger_record_at(text: str, lane: str) -> datetime | None:
    """The newest TERMINAL row of ``lane``."""
    newest: datetime | None = None
    needle = f"lane={lane}"
    for line in text.splitlines():
        if needle not in line or "TERMINAL" not in line:
            continue
        row = parse_row(line)
        stamp = _parse(line[:20])
        if row is None or stamp is None or row.row_type != "TERMINAL":
            continue
        if row.token("lane") == lane and (newest is None or stamp > newest):
            newest = stamp
    return newest


def receipt_record_at(
    receipts: Path, lane: str, phases: tuple[str, ...]
) -> tuple[datetime | None, bool]:
    """The newest receipt of ``lane`` in a run phase, and whether the file was readable."""
    try:
        text = receipts.read_text()
    except OSError:
        return None, False
    newest: datetime | None = None
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("workflow") != lane:
            continue
        if row.get("phase") not in phases:
            continue
        stamp = _parse(str(row.get("ts", "")))
        if stamp is not None and (newest is None or stamp > newest):
            newest = stamp
    return newest, True
