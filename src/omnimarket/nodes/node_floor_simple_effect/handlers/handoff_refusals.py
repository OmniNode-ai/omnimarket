# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the floor reads of the landing controller's refusals of handed-off pull requests (OMN-23242).

Three sources, each reduced here and counted by the compute node: the ledger's hand-off MSG rows (a MSG to the controller with
a ``pr=`` cell, which names the lane that handed the pull request over) and the controller's own parent MSG rows (written from
the controller lane, naming ``pr=``, ``head=`` and ``gate=``); the controller's ``decisions.jsonl`` (one ``merge`` row per tick
a merge stays deferred); and its newest admission row per pull request (``kept_out``). A source that cannot be read makes the
whole reading unread, with the reason: a smaller count from half the sources would be a wrong number drawn as a right one.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from omnimarket.models.floor_17a.model_floor_handoff_refusals import (
    ModelHandoffLedgerFacts,
    ModelHandoffRefusalsIn,
    ModelHandoffRefusalsOverlay,
    ModelHandoffRow,
    ModelParentMsgRow,
    ModelRefusalRow,
)
from omnimarket.models.model_reading import ModelLedgerEvidenceRow

MSG_ROW_TYPE = "MSG"
KEPT_OUT_GATE = "KEPT_OUT"
# The controller's gate words, spelled as its parent MSG spells them (its ``gate_of``): the floor keys a deferral to the
# parent MSG that names the same pull request, head and gate.
_GATE_PREFIXES = (
    ("release premise", "RELEASE_PREMISE"),
    ("node handover", "NODE_HANDOVER"),
    ("strict base", "STRICT_BASE"),
)


def short_pr(subject: str) -> str:
    """``repo#n`` of ``owner/repo#n`` or ``repo#n``, backticks off."""
    return subject.strip().strip("`").rpartition("/")[2]


def gate_of(detail: str) -> str:
    """The gate word of a merge deferral's detail, as the controller's parent MSG names it."""
    d = " ".join(str(detail).split())
    if d.startswith("criteria check not run"):
        return "CRITERIA_CHECK_NOT_RUN"
    if "CRITERIA_UNREADABLE" in d:
        return "CRITERIA_UNREADABLE"
    if "CRITERIA_DRIFT" in d:
        return "CRITERIA_DRIFT"
    return next(
        (gate for prefix, gate in _GATE_PREFIXES if d.startswith(prefix)),
        "MERGE_DEFERRED",
    )


def handoff_ledger_facts(
    rows: Iterable[ModelLedgerEvidenceRow], controller_lane: str
) -> ModelHandoffLedgerFacts:
    """The newest hand-off of each pull request and every parent MSG the controller wrote, from MSG rows."""
    handed: dict[str, ModelHandoffRow] = {}
    told: dict[tuple[str, str, str], ModelParentMsgRow] = {}
    for row in sorted(rows, key=lambda r: r.stamp):
        if row.row_type != MSG_ROW_TYPE:
            continue
        pr = short_pr(row.cells.get("pr", ""))
        if "#" not in pr:
            continue
        sender = row.cells.get("from") or row.lane or ""
        targets = {t.strip().strip("`") for t in row.cells.get("to", "").split(",")}
        if sender == controller_lane:
            head, gate = row.cells.get("head", ""), row.cells.get("gate", "")
            if head and gate:
                told[(pr, head, gate)] = ModelParentMsgRow(pr=pr, head=head, gate=gate)
        elif controller_lane in targets and sender:
            handed[pr] = ModelHandoffRow(
                pr=pr, head=row.cells.get("head", ""), lane=sender
            )
    return ModelHandoffLedgerFacts(
        handed=tuple(handed[k] for k in sorted(handed)),
        told=tuple(told[k] for k in sorted(told)),
    )


def read_decision_tail(path: Path, tail_bytes: int) -> list[dict[str, Any]] | None:
    """The controller's decision rows of the newest ``tail_bytes`` of ``decisions.jsonl``, oldest first, or ``None`` when the
    journal cannot be read. An absent journal is unread too: the controller writes it every tick."""
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - tail_bytes))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    rows: list[dict[str, Any]] = []
    for line in tail.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _time(text: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def deferral_rows(
    decisions: Iterable[Mapping[str, Any]],
    now: datetime,
    overlay: ModelHandoffRefusalsOverlay,
) -> tuple[ModelRefusalRow, ...]:
    """The merge deferrals still holding: per pull request, head and gate since the pull request's last merge row that was not a
    deferral, the ticks it was deferred (distinct ``tick`` numbers, else distinct stamps). A deferral whose newest row is older
    than ``current_window_s`` ended (the controller writes one row a tick while it holds). A pipeline tick-budget deferral is not
    a refusal and neither is a gate of ``queue_gates``."""
    groups: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    for row in decisions:
        if row.get("kind") != "merge" or "#" not in str(row.get("subject") or ""):
            continue
        pr = short_pr(str(row["subject"]))
        if row.get("outcome") != "deferred":
            groups.pop(
                pr, None
            )  # a merge, a rerun or a refusal of another kind ends the deferral run
            continue
        if row.get("pipeline"):
            continue
        head = str(row.get("head_sha") or "")
        gate = gate_of(str(row.get("detail") or ""))
        if not head or gate in overlay.queue_gates:
            continue
        stamp = _time(row.get("ts"))
        if stamp is None:
            continue
        entry = groups.setdefault(pr, {}).setdefault(
            (head, gate), {"ticks": set(), "newest": stamp}
        )
        entry["ticks"].add(str(row.get("tick", row.get("ts"))))
        entry["newest"] = max(entry["newest"], stamp)
    current = now - timedelta(seconds=overlay.current_window_s)
    return tuple(
        ModelRefusalRow(
            pr=pr,
            head=head,
            gate=gate,
            kind="deferred",
            ticks=len(entry["ticks"]),
            newest_at=entry["newest"].strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        for pr, by_key in sorted(groups.items())
        for (head, gate), entry in sorted(by_key.items())
        if entry["newest"] >= current
    )


def kept_out_rows(
    admissions: Mapping[str, Mapping[str, Any]],
) -> tuple[ModelRefusalRow, ...]:
    """Every pull request whose newest admission row keeps it out of landing."""
    return tuple(
        ModelRefusalRow(
            pr=short_pr(pr),
            head=str(row.get("head") or ""),
            gate=KEPT_OUT_GATE,
            kind="kept_out",
            newest_at=str(row.get("ts") or ""),
        )
        for pr, row in sorted(admissions.items())
        if row.get("event") == "kept_out" and row.get("head")
    )


def handoff_refusals_reading(
    *,
    ledger: ModelHandoffLedgerFacts | None,
    ledger_error: str,
    decisions: list[dict[str, Any]] | None,
    admissions: Mapping[str, Mapping[str, Any]] | None,
    now: datetime,
    overlay: ModelHandoffRefusalsOverlay,
) -> ModelHandoffRefusalsIn:
    """The cycle's reading, or the first source that could not be read as the reason."""
    if ledger is None:
        return ModelHandoffRefusalsIn(
            status="unread",
            reason=f"ledger hand-off rows unread: {ledger_error or 'not read'}",
        )
    if decisions is None:
        return ModelHandoffRefusalsIn(
            status="unread", reason="the controller's decisions.jsonl was not readable"
        )
    if admissions is None:
        return ModelHandoffRefusalsIn(
            status="unread", reason="the controller's admissions.jsonl was not readable"
        )
    return ModelHandoffRefusalsIn(
        status="read",
        ledger=ledger,
        refusals=(
            *deferral_rows(decisions, now, overlay),
            *kept_out_rows(admissions),
        ),
    )
