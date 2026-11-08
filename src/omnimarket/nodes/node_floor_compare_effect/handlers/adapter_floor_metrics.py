# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Flatten the floor's published documents into the numbers the comparison keeps (OMN-23018).

Only a number that was read is returned. A number the document marks as not read, or cannot parse, is left out, so the
history never holds a zero that was really unread.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from omnimarket.nodes.node_floor_compare_effect.models.model_floor_compare import (
    ModelFloorCompareRuntime,
    ModelSnapshot,
)

_LEADING_NUMBER = re.compile(r"^\s*(\d+(?:\.\d+)?)")


def parse_stamp(value: object) -> datetime | None:
    """A UTC datetime from an ISO string, or ``None`` when it is not one."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _number(raw: object) -> float | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        return float(raw)
    if isinstance(raw, str):
        match = _LEADING_NUMBER.match(raw)
        return float(match.group(1)) if match else None
    return None


def snapshot_from_floor_simple(
    doc: Mapping[str, Any], rt: ModelFloorCompareRuntime, *, source: str
) -> ModelSnapshot | None:
    """The numbers of one ``omni.floor-simple/1`` document, or ``None`` when it carries no time."""
    at = parse_stamp(doc.get("generated_at"))
    if at is None:
        return None
    values: dict[str, float] = {}
    labels: dict[str, str] = {}
    for item in doc.get("numbers") or ():
        if not isinstance(item, Mapping) or item.get("read") is not True:
            continue
        number = _number(item.get("value"))
        if number is None:
            continue
        key = "slots_busy" if item.get("key") == "slots" else str(item.get("key"))
        values[key] = number
    rule = doc.get("red_rule")
    if isinstance(rule, Mapping) and (red := _number(rule.get("red_rows"))) is not None:
        values["red_rows"] = red
    cost = re.compile(rt.stoppage_cost_pattern)
    for cause in doc.get("causes") or ():
        if not isinstance(cause, Mapping) or not cause.get("id"):
            continue
        cid = str(cause["id"])
        if (size := _number(cause.get("size"))) is not None:
            values[f"cause:{cid}"] = size
            labels[f"cause:{cid}"] = str(cause.get("title") or cid)
        if cid.startswith("stoppage:") and (
            hit := cost.search(str(cause.get("detail") or ""))
        ):
            values[f"stoppage_cost:{cid}"] = float(hit.group(1))
            labels[f"stoppage_cost:{cid}"] = str(cause.get("title") or cid)
    alarms = doc.get("alarms")
    if isinstance(alarms, list):
        values["alarms_total"] = float(len(alarms))
        for alarm in alarms:
            if isinstance(alarm, Mapping) and alarm.get("source_id"):
                key = f"alarm:{alarm['source_id']}"
                values[key] = values.get(key, 0.0) + 1.0
                labels[key] = str(alarm.get("title") or alarm["source_id"])
    for piece in doc.get("pieces") or ():
        if isinstance(piece, Mapping) and piece.get("draw") in rt.piece_rank:
            key = f"piece:{piece.get('piece')}"
            values[key] = float(rt.piece_rank[str(piece["draw"])])
            labels[key] = f"{piece.get('piece')}, {piece.get('name', '')}".rstrip(", ")
    for lab in doc.get("lab_state") or ():
        if (
            isinstance(lab, Mapping)
            and lab.get("draw") in rt.piece_rank
            and lab.get("id")
        ):
            key = f"lab:{lab['id']}"
            values[key] = float(rt.piece_rank[str(lab["draw"])])
            labels[key] = str(lab.get("title") or lab["id"])
    return ModelSnapshot(at=at, source=source, values=values, labels=labels)


def snapshot_from_factory_record(
    doc: Mapping[str, Any], rt: ModelFloorCompareRuntime, *, source: str
) -> ModelSnapshot | None:
    """The stoppages and slots of one factory floor record (``factory-floor.json``), for backfill."""
    at = parse_stamp(doc.get("generated_at"))
    floor = doc.get("floor")
    if at is None or not isinstance(floor, Mapping):
        return None
    values: dict[str, float] = {}
    labels: dict[str, str] = {}
    for item in floor.get("stoppages") or ():
        if not isinstance(item, Mapping) or not item.get("name"):
            continue
        cid = f"stoppage:{item['name']}"
        title = str(item["name"]).replace("-", " ").replace(":", ": ")
        if (n := _number(item.get("occurrences"))) is not None:
            values[f"cause:{cid}"] = n
            labels[f"cause:{cid}"] = title
        if (c := _number(item.get("cost_minutes"))) is not None:
            values[f"stoppage_cost:{cid}"] = c
            labels[f"stoppage_cost:{cid}"] = title
    bays = [b for b in floor.get("bays") or () if isinstance(b, Mapping)]
    if bays and not any(b.get("stale") for b in bays):
        values["slots_busy"] = float(sum(int(b.get("running") or 0) for b in bays))
    return ModelSnapshot(at=at, source=source, values=values, labels=labels)
