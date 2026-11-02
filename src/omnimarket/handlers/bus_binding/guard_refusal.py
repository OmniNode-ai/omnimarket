# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Settle a ledger guard's refusal of a replayed append in code, or leave the case open once (OMN-21959).

A refused outbox request is final, and the queue's fix for the class is "correct the row and append it again".
Seven recorded cases of class ``bus-binding-outbox-refused`` were refused not for a request-id clash but by a
ledger guard on the TERMINAL they carried: the friction guard (OMN-18274, a cited friction with no FRICTION row)
and the worktree cell guard (OMN-20148). Whether the row can be appended again is a fact about the ledger now:

* every row the refusal lost is already in the ledger (replayed by hand with its own stamp), or the lane's CLAIM
  is already closed by a TERMINAL: the lost row is superseded, nothing is appended;
* the cause has since been made good (the lane has a FRICTION row, the removed tree is gone, the cell names the
  CLAIM's worktree): the refused rows are appended again, unchanged, as a new request;
* anything else needs a fact only the lane has (a cost, which of two worktrees was used): the case stays open
  with that fact named. Code never invents it.

A row marked as a decision is never resolved here. The ledger, the refused request's body, the filesystem and
the append are four ports the caller binds; this module reads and writes none of them itself.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from omnimarket.handlers.bus_binding import recovery
from omnimarket.models.bus_binding.model_guard_refusal import (
    EnumGuardRefusal,
    ModelGuardRefusal,
    ModelGuardResolution,
)
from omnimarket.nodes.node_exception_queue_effect.container.container_exception_queue_effect import (
    create_handler_exception_emit,
    create_handler_exception_queue,
    create_handler_exception_resolve,
)
from omnimarket.nodes.node_exception_queue_effect.models import (
    ModelExceptionEmitRequest,
    ModelExceptionQueueRequest,
    ModelExceptionResolveRequest,
    ModelExceptionRow,
)

_FRICTION_MARK = "OMN-18274 friction guard rejected append"
_WORKTREE_MARK = "OMN-20148 worktree cell rejected append"
_LANE_IN_EVIDENCE = re.compile(r"no FRICTION row for lane '(?P<lane>[^']+)'")
_LANE_IN_KEY = re.compile(r"(?:^|\s)lane=(?P<lane>\S+)")
_LOST_STAMP = re.compile(r"rows lost: [A-Z,]+ stamped (?P<stamp>[0-9TZ:\-]+)")
_LOST_ROWS = re.compile(
    r"rows lost: (?P<kinds>[A-Z,]+) stamped (?P<stamps>[0-9TZ:,\-]+)"
)
_REMOVED_EXISTS = re.compile(
    r"worktree=removed:(?P<path>\S+) but that path still exists"
)
_KEPT_MISMATCH = re.compile(
    r"worktree=kept:(?P<path>[^:\s]+)(?::\S*)? is not the worktree the CLAIM it closes named"
)
_REFUSED_PATH = re.compile(r"kept the refused request at (?P<path>\S+)")
_CLOSES = re.compile(r"closes-CLAIM=(?P<stamp>\S+)")
_WORKTREE_CELL = re.compile(r"\| worktree=(?P<value>[^|\s]+)")
_FRICTION_CELL = re.compile(r"\| friction=(?P<value>[^|]+)")


@dataclass(frozen=True)
class GuardPorts:
    """What the actor may ask of the world. None of them raises for an unreachable thing: it answers no."""

    lane_rows: Callable[
        [str], Sequence[str]
    ]  # every ledger row, archive rolls included, naming lane=<lane>
    refused_rows: Callable[
        [str], str | None
    ]  # the rows of the refused request kept at a path; None when unreadable here
    path_exists: Callable[[str], bool]
    append: Callable[
        [str], bool
    ]  # append rows as a new request; True when the ledger accepted them


def _stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def read_guard_refusal(row: Mapping[str, object]) -> ModelGuardRefusal | None:
    """The guard refusal a queue case records, from its evidence; None for any other refusal."""
    evidence = str(row.get("evidence", ""))
    key = str(row.get("key", ""))
    if _FRICTION_MARK in evidence:
        guard, cell = EnumGuardRefusal.FRICTION_GUARD, ""
    elif _WORKTREE_MARK in evidence:
        removed = _REMOVED_EXISTS.search(evidence)
        kept = _KEPT_MISMATCH.search(evidence)
        if removed:
            guard, cell = EnumGuardRefusal.WORKTREE_REMOVED_EXISTS, removed["path"]
        elif kept:
            guard, cell = EnumGuardRefusal.WORKTREE_KEPT_MISMATCH, kept["path"]
        else:
            return None
    else:
        return None
    lane_match = _LANE_IN_EVIDENCE.search(evidence) or _LANE_IN_KEY.search(key)
    if lane_match is None:
        return None
    stamp = _LOST_STAMP.search(evidence)
    lost = _LOST_ROWS.search(evidence)
    kinds = lost["kinds"].split(",") if lost else []
    stamps = lost["stamps"].split(",") if lost else []
    path = _REFUSED_PATH.search(str(row.get("tried", "")))
    return ModelGuardRefusal(
        guard=guard,
        lane=lane_match["lane"],
        lost_stamp=stamp["stamp"] if stamp else "",
        refused_path=path["path"] if path else "",
        cell=cell,
        lost_rows=tuple(f"{k}@{s}" for k, s in zip(kinds, stamps, strict=True))
        if len(kinds) == len(stamps)
        else (),
    )


def _kind(line: str) -> str:
    cells = line.split(" | ", 2)
    return cells[1].strip() if len(cells) > 1 else ""


def _stamp_of(line: str) -> str:
    return line.split(" | ", 1)[0].strip()


def _claim_stamp(rows: Sequence[str], lost_stamp: str) -> str:
    """The CLAIM the lost TERMINAL closed: the latest one before it, else the latest there is."""
    stamps = sorted(_stamp_of(r) for r in rows if _kind(r) == "CLAIM")
    before = [s for s in stamps if not lost_stamp or s <= lost_stamp]
    return (before or stamps or [""])[-1]


def _again(ports: GuardPorts, body: str, held: str) -> tuple[bool, str]:
    if ports.append(body):
        return (
            True,
            f"{held}; appended the refused rows again, unchanged, as a new request",
        )
    return False, f"{held}, but the ledger refused the append again"


def _close(refusal: ModelGuardRefusal, ports: GuardPorts) -> tuple[bool, str]:
    """(resolved, how or what is still missing) for one guard refusal."""
    lane = refusal.lane
    rows = ports.lane_rows(lane)
    landed = {f"{_kind(r)}@{_stamp_of(r)}" for r in rows}
    if refusal.lost_rows and all(row in landed for row in refusal.lost_rows):
        return True, (
            f"the ledger already holds every row the refusal lost for lane {lane} "
            f"({', '.join(refusal.lost_rows)}); nothing is appended again"
        )
    claim = _claim_stamp(rows, refusal.lost_stamp)
    if not claim:
        return False, (
            f"the ledger holds no CLAIM for lane {lane}, so no row of it can be told closed or corrected; "
            "the lane's claim was lost with its TERMINAL"
        )
    for line in rows:
        closes = _CLOSES.search(line)
        if _kind(line) == "TERMINAL" and closes and closes["stamp"] == claim:
            return True, (
                f"the ledger already holds a TERMINAL for lane {lane} closing the CLAIM of {claim}; "
                "the lost TERMINAL is superseded"
            )
    body = ports.refused_rows(refusal.refused_path) if refusal.refused_path else None
    if body is None:
        return False, (
            f"the refused request at {refusal.refused_path or 'an unnamed path'} is not readable here, "
            "so the rows cannot be corrected unseen"
        )
    if refusal.guard is EnumGuardRefusal.FRICTION_GUARD:
        if any(_kind(r) == "FRICTION" for r in rows):
            return _again(
                ports, body, f"the ledger now holds a FRICTION row for lane {lane}"
            )
        friction = _FRICTION_CELL.search(body)
        return False, (
            f"lane {lane} asserted friction={friction['value'].strip() if friction else 'unreadable'} "
            "and the ledger holds no FRICTION row for it; the row needs a ticket and a cost estimate "
            "that only the lane can give, and code does not invent a cost"
        )
    if refusal.guard is EnumGuardRefusal.WORKTREE_REMOVED_EXISTS:
        if ports.path_exists(refusal.cell):
            return False, (
                f"worktree {refusal.cell} still exists; run onex-lane-closeout --lane {lane} "
                f"--worktree {refusal.cell} and use the cell it prints"
            )
        return _again(ports, body, f"worktree {refusal.cell} no longer exists")
    claimed = next(
        (
            m["value"]
            for line in rows
            if _kind(line) == "CLAIM"
            and _stamp_of(line) == claim
            and (m := _WORKTREE_CELL.search(line))
        ),
        "",
    )
    if claimed and claimed == refusal.cell:
        return _again(ports, body, f"the CLAIM of {claim} names worktree {claimed}")
    return False, (
        f"TERMINAL names kept:{refusal.cell} but the CLAIM of {claim} named "
        f"{claimed or 'no worktree'}; which tree the lane used is the lane's to say"
    )


def settle_guard_case(
    state_dir: Path,
    row: Mapping[str, object],
    ports: GuardPorts,
    *,
    now: datetime,
    emit_open: bool = True,
) -> ModelGuardResolution | None:
    """Resolve one guard-refusal case in code, or leave it open; None when it is not a guard refusal.

    ``emit_open=False`` leaves an unsettled case as it is instead of emitting it again: the replay path settles
    on every pass and must not add an occurrence for a case it already reported.
    """
    refusal = read_guard_refusal(row)
    if refusal is None:
        return None
    key = str(row.get("key", ""))
    if row.get("decision"):
        return ModelGuardResolution(
            lane=refusal.lane,
            key=key,
            resolved=False,
            how="decision row: left to the operator",
            emitted="",
        )
    resolved, how = _close(refusal, ports)
    queue_dir = state_dir / "exception-queue"
    if resolved:
        case_id = str(row.get("id", ""))
        if case_id:
            create_handler_exception_resolve().handle(
                ModelExceptionResolveRequest(
                    queue_dir, case_id, recovery.SOURCE, how, _stamp(now)
                )
            )
        return ModelGuardResolution(
            lane=refusal.lane, key=key, resolved=True, how=how, emitted=""
        )
    if not emit_open:
        return ModelGuardResolution(
            lane=refusal.lane, key=key, resolved=False, how=how, emitted=""
        )
    emitted = create_handler_exception_emit().handle(
        ModelExceptionEmitRequest(
            state_dir=queue_dir,
            row=ModelExceptionRow(
                cls=recovery.REFUSED_CLASS,
                key=key,
                source=recovery.SOURCE,
                evidence=str(row.get("evidence", "")),
                tried=how,
                proposed_fix=str(row.get("proposed_fix", ""))
                or "correct the row and append it again; the refused request is final",
                decision=False,
            ),
            now=_stamp(now),
        )
    )
    return ModelGuardResolution(
        lane=refusal.lane,
        key=key,
        resolved=False,
        how=how,
        emitted=f"{emitted.status}:{emitted.id}",
    )


def settle_guard_refusals(
    state_dir: Path, ports: GuardPorts, *, now: datetime, emit_open: bool = True
) -> list[ModelGuardResolution]:
    """Settle every open guard-refusal case of the queue under ``state_dir``; other open cases are not touched."""
    queue = create_handler_exception_queue().handle(
        ModelExceptionQueueRequest(state_dir / "exception-queue", "orchestrator")
    )
    results: list[ModelGuardResolution] = []
    for row in queue.rows:
        if row.get("cls") != recovery.REFUSED_CLASS:
            continue
        settled = settle_guard_case(state_dir, row, ports, now=now, emit_open=emit_open)
        if settled is not None:
            results.append(settled)
    return results
