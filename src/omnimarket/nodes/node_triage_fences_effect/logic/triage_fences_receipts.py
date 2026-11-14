# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the lane runner receipts and report which lanes are running (OMN-20682).

A receipt is ``<receipt_dir>/<lane>/rlane-*.json`` (never ``*.job.json``). A lane
is running when its newest receipt, ordered by ``started_at`` then file name, has
``status == "running"``, ``final`` is not True, and ``started_at`` is not older
than ``max_age_minutes`` before ``now``. The lane name is the receipt's ``lane``
field, else the folder name. An unreadable or non-object receipt adds an error
string and never blanks the other lanes. A missing ``receipt_dir`` returns no
lanes and one error. The function takes no clock; ``now`` is passed in.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_NEVER = datetime.min.replace(tzinfo=UTC)


def running_lanes(
    receipt_dir: Path, now: datetime, max_age_minutes: float
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (sorted running lane names, error strings) from the runner receipts."""
    if not receipt_dir.is_dir():
        return (), (f"receipt_dir {receipt_dir} does not exist",)
    cutoff = now - timedelta(minutes=max_age_minutes)
    lanes: dict[str, tuple[datetime, str, dict[str, Any]]] = {}
    errors: list[str] = []
    for lane_dir in sorted(receipt_dir.iterdir()):
        if not lane_dir.is_dir():
            continue
        for receipt in sorted(lane_dir.glob("rlane-*.json")):
            if receipt.name.endswith(".job.json"):
                continue
            try:
                raw = json.loads(receipt.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                errors.append(f"{receipt.name}: unreadable ({type(exc).__name__})")
                continue
            if not isinstance(raw, dict):
                errors.append(f"{receipt.name}: not a JSON object")
                continue
            started_raw = raw.get("started_at")
            try:
                started = (
                    datetime.fromisoformat(str(started_raw).replace("Z", "+00:00"))
                    if started_raw is not None
                    else None
                )
            except ValueError:
                started = None
            if started is not None and started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            key = (started or _NEVER, receipt.name)
            lane_name = str(raw.get("lane") or lane_dir.name)
            current = lanes.get(lane_name)
            if current is None or key > (current[0], current[1]):
                lanes[lane_name] = (started or _NEVER, receipt.name, raw)
    running: list[str] = []
    for lane_name, (started, _, raw) in lanes.items():
        if (
            raw.get("status") == "running"
            and raw.get("final") is not True
            and started != _NEVER
            and started >= cutoff
        ):
            running.append(lane_name)
    return tuple(sorted(running)), tuple(errors)


__all__ = ["running_lanes"]
