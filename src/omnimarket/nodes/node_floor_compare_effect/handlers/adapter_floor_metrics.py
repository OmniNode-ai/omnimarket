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
        # The age of the piece's last golden event, in hours (OMN-23081): read only when the floor read it.
        if (
            isinstance(piece, Mapping)
            and (age := _number(piece.get("golden_age_s"))) is not None
        ):
            gkey = f"piece_golden_age_h:{piece.get('piece')}"
            values[gkey] = round(age / 3600.0, 3)
            labels[gkey] = f"{piece.get('piece')}, {piece.get('name', '')}".rstrip(", ")
    for lab in doc.get("lab_state") or ():
        if (
            isinstance(lab, Mapping)
            and lab.get("draw") in rt.piece_rank
            and lab.get("id")
        ):
            key = f"lab:{lab['id']}"
            values[key] = float(rt.piece_rank[str(lab["draw"])])
            labels[key] = str(lab.get("title") or lab["id"])
    # The floor lists these collections in full for a document that read them: the producer leaves out a pull request cause
    # with no pull requests and a lab cause that is WORKING, and the alarms list holds every alarm that is up. A key under
    # them that the document does not hold was read as absent (zero), not left unread.
    complete: list[str] = []
    numbers = {
        str(i.get("key")): i
        for i in doc.get("numbers") or ()
        if isinstance(i, Mapping) and i.get("read") is True
    }
    if "open_prs" in numbers and isinstance(doc.get("causes"), list):
        complete.append("cause:prs:")
    if doc.get("lab_state"):
        complete.append("cause:lab:")
    if isinstance(alarms, list):
        complete.append("alarm:")
    return ModelSnapshot(
        at=at,
        source=source,
        values=values,
        labels=labels,
        complete=tuple(complete),
    )


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
        if (
            cid not in rt.estimated_cost_stoppages
            and (c := _number(item.get("cost_minutes"))) is not None
        ):
            values[f"stoppage_cost:{cid}"] = c
            labels[f"stoppage_cost:{cid}"] = title
    # As the live floor reads slots: the hosts whose bay is stale are left out, the rest are summed, and a reading with no
    # capacity among the hosts that were read is unread.
    read = [
        b
        for b in floor.get("bays") or ()
        if isinstance(b, Mapping) and not b.get("stale")
    ]
    if sum(int(b.get("slots") or 0) for b in read) > 0:
        values["slots_busy"] = float(sum(int(b.get("running") or 0) for b in read))
    return ModelSnapshot(at=at, source=source, values=values, labels=labels)


def measures_from_factory_record(
    doc: Mapping[str, Any], rt: ModelFloorCompareRuntime, *, source: str
) -> ModelSnapshot | None:
    """The piece measures one factory floor record holds (OMN-23081): lab slots free over the hosts read fresh, and how far
    the newest ledger row read lags the record's own read. A value the record did not read is left out."""
    del rt
    at = parse_stamp(doc.get("generated_at"))
    floor = doc.get("floor")
    if at is None or not isinstance(floor, Mapping):
        return None
    values: dict[str, float] = {}
    # A host read stale has no slot available to a lane, so only fresh hosts count; with no fresh host it is unread.
    fresh = [
        b
        for b in floor.get("bays") or ()
        if isinstance(b, Mapping) and not b.get("stale")
    ]
    frees = [_number(b.get("free")) for b in fresh]
    if fresh and all(f is not None for f in frees):
        values["slots_free"] = float(sum(f for f in frees if f is not None))
    sources = doc.get("sources")
    newest = (
        parse_stamp(sources.get("ledger_newest_row"))
        if isinstance(sources, Mapping)
        else None
    )
    if newest is not None:
        values["ledger_read_lag_s"] = max(0.0, (at - newest).total_seconds())
    return ModelSnapshot(at=at, source=source, values=values)
