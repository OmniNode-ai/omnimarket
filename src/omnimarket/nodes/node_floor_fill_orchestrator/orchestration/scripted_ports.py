# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scripted adapters of every seam: the in-memory default profile and the golden tests run on these.

Each records its calls so a test counts reads, mints, creates and starts, and each can be told to fail
the way its real counterpart fails (an unreadable source, a refused mint, a failed start).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, ClassVar

from pydantic import ValidationError

from omnimarket.models.floor_fill import (
    ModelFloorEntryTicketVerdict,
    ModelFloorFillCandidatesRequest,
    ModelFloorRecordRead,
    ModelFloorTaskPayloadVerdict,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    FloorFillUnreadableError,
    ModelLaneProcessStartResult,
    ModelLaneStart,
    ModelLaneStartSpec,
    ModelMintResult,
    ModelRunOutcome,
    lane_start_from_spec,
)

_ = ModelFloorFillCandidatesRequest  # the request type the fixture floors are valid for


class ScriptedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, **delta: float) -> None:
        self._now += timedelta(**delta)


class ScriptedFacts:
    """The floor, plan texts, brake and stop signal; any of them can be made unreadable."""

    synchronous_execution: ClassVar[bool] = True

    def __init__(
        self,
        floor: str | None,
        *,
        plans: Mapping[str, str] | None = None,
        landing_only: bool = False,
        stop_cause: str | None = None,
        unreadable: frozenset[str] = frozenset(),
    ) -> None:
        self.floor = floor
        self.plans = dict(plans or {})
        self.landing_only_value = landing_only
        self.stop_cause = stop_cause
        self.unreadable = unreadable
        self.calls: list[str] = []

    def read_floor(self, url: str) -> str:
        self.calls.append("floor")
        if "floor" in self.unreadable or self.floor is None:
            raise FloorFillUnreadableError("no-answer")
        return self.floor

    def read_plan(self, plans_root: str, line_id: str) -> str:
        self.calls.append(f"plan:{line_id}")
        if f"plan:{line_id}" in self.unreadable:
            raise FloorFillUnreadableError("plan-absent")
        return self.plans.get(line_id, "")

    def landing_only(self) -> bool:
        self.calls.append("brake")
        if "brake" in self.unreadable:
            raise FloorFillUnreadableError("no-reading")
        return self.landing_only_value

    def stop_refuses(self, task_class: str) -> str | None:
        self.calls.append("stop-signal")
        if "stop-signal" in self.unreadable:
            raise FloorFillUnreadableError("no-reading")
        return self.stop_cause


class ScriptedPool:
    def __init__(self, free: int, *, unreadable: bool = False) -> None:
        self.free = free
        self.unreadable = unreadable

    def free_slots(self) -> int:
        if self.unreadable:
            raise FloorFillUnreadableError("no host answered")
        return self.free


class ScriptedLedger:
    """CLAIM and TERMINAL rows by lane; ``claim`` is what a started lane's leader appends."""

    def __init__(self, *, unreadable: bool = False) -> None:
        self.claims: set[tuple[str, str]] = set()
        self.terminals: set[str] = set()
        self.unreadable = unreadable
        self.reads = 0

    def claim(self, lane: str, key: str) -> None:
        self.claims.add((lane, key))

    def terminal(self, lane: str) -> None:
        self.terminals.add(lane)

    def claim_found(self, lane: str, key: str) -> bool:
        self.reads += 1
        if self.unreadable:
            raise FloorFillUnreadableError("ledger-unreadable")
        return (lane, key) in self.claims

    def lane_ended(self, lane: str) -> bool:
        self.reads += 1
        if self.unreadable:
            raise FloorFillUnreadableError("ledger-unreadable")
        return lane in self.terminals


class ScriptedTickets:
    """T11's seam: every ready task of the record has an ok payload unless listed; the mint can refuse or time out."""

    def __init__(
        self,
        *,
        existing: Mapping[str, str] | None = None,
        payload_refused: frozenset[str] = frozenset(),
        mint_refused: Mapping[str, str] | None = None,
        mint_unavailable: frozenset[str] = frozenset(),
    ) -> None:
        self.existing = dict(existing or {})
        self.payload_refused = payload_refused
        self.mint_refused = dict(mint_refused or {})
        self.mint_unavailable = mint_unavailable
        self.calls: list[str] = []
        self.created: list[str] = []
        self.tickets: dict[str, str] = {}

    def payload_verdicts(
        self, floor_record: str, plan_texts: Mapping[str, str | None]
    ) -> Sequence[ModelFloorTaskPayloadVerdict]:
        self.calls.append("payload")
        try:
            record = ModelFloorRecordRead.model_validate_json(floor_record)
        except (ValidationError, ValueError):
            return ()
        out: list[ModelFloorTaskPayloadVerdict] = []
        for line in record.lines:
            for station in line.stations:
                for item in station.items:
                    if item.state != "prep" or not item.note.startswith("ready"):
                        continue
                    key = f"floor:{line.line_id}:{item.id}"
                    if key in self.payload_refused:
                        out.append(
                            ModelFloorTaskPayloadVerdict(
                                line_id=line.line_id,
                                task_id=item.id,
                                ok=False,
                                cause="needs-spec:falsifier",
                            )
                        )
                    else:
                        out.append(
                            ModelFloorTaskPayloadVerdict(
                                line_id=line.line_id, task_id=item.id, ok=True
                            )
                        )
        return out

    def entry_ticket_verdicts(
        self, tickets: Mapping[str, str]
    ) -> Sequence[ModelFloorEntryTicketVerdict]:
        self.calls.append("ticket-read")
        out = []
        for key, ticket in tickets.items():
            _, line_id, task_id = key.split(":", 2)
            out.append(
                ModelFloorEntryTicketVerdict(
                    line_id=line_id, task_id=task_id, status="usable", ticket=ticket
                )
            )
        return out

    def mint(self, key: str) -> ModelMintResult:
        self.calls.append(f"mint:{key}")
        if key in self.mint_unavailable:
            return ModelMintResult(state="unavailable", cause="deadline")
        if key in self.mint_refused:
            return ModelMintResult(state="refused", cause=self.mint_refused[key])
        if key in self.existing:
            return ModelMintResult(state="ticket", ticket=self.existing[key])
        if key in self.tickets:
            return ModelMintResult(state="ticket", ticket=self.tickets[key])
        ticket = f"T-{len(self.created) + 1}"
        self.created.append(key)
        self.tickets[key] = ticket
        return ModelMintResult(state="ticket", ticket=ticket)


class ScriptedStarter:
    """The process effect for detached starts: records each start, answers ``failed`` for the keys told to, and can run a hook."""

    synchronous_execution: ClassVar[bool] = True

    def __init__(
        self,
        *,
        fail_keys: frozenset[str] = frozenset(),
        fail_all: bool = False,
        cause: str = "exit-3",
        after_start: Callable[[ModelLaneStart], None] | None = None,
    ) -> None:
        self.fail_keys = fail_keys
        self.fail_all = fail_all
        self.cause = cause
        self.after_start = after_start
        self.starts: list[ModelLaneStart] = []

    def start_detached(self, spec: ModelLaneStartSpec) -> ModelLaneProcessStartResult:
        start = lane_start_from_spec(spec)
        if start is None:
            return ModelLaneProcessStartResult(
                state="failed", cause="not-a-floor-start"
            )
        self.starts.append(start)
        if self.fail_all or start.key in self.fail_keys:
            return ModelLaneProcessStartResult(state="failed", cause=self.cause)
        if self.after_start is not None:
            self.after_start(start)
        return ModelLaneProcessStartResult(state="started", pid=len(self.starts))

    def run_bounded(
        self, spec: ModelLaneStartSpec, timeout_seconds: float
    ) -> ModelRunOutcome:
        return ModelRunOutcome(state="failed", cause="not-a-command-runner")


class ScriptedBriefs:
    """The previous reports by path, and the briefs a continuation start saved by lane."""

    def __init__(
        self,
        reports: Mapping[str, str] | None = None,
        unreadable_reports: frozenset[str] = frozenset(),
        fail_save: bool = False,
    ) -> None:
        self.reports = dict(reports or {})
        self.unreadable_reports = unreadable_reports
        self.fail_save = fail_save
        self.saved: dict[str, str] = {}

    def report_text(self, report_path: str) -> str | None:
        if report_path in self.unreadable_reports:
            raise FloorFillUnreadableError("report-unreadable")
        return self.reports.get(report_path)

    def save_brief(self, lane: str, text: str) -> str:
        if self.fail_save:
            raise FloorFillUnreadableError("brief-unwritable")
        self.saved[lane] = text
        return f"scripted://briefs/{lane}.md"


class ScriptedCommandForward:
    """The process effect for the forward: records argv and timeout, answers from a script."""

    synchronous_execution: ClassVar[bool] = True

    def __init__(self, outcome: ModelRunOutcome) -> None:
        self.outcome = outcome
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def start_detached(self, spec: ModelLaneStartSpec) -> ModelLaneProcessStartResult:
        return ModelLaneProcessStartResult(state="failed", cause="not-a-starter")

    def run_bounded(
        self, spec: ModelLaneStartSpec, timeout_seconds: float
    ) -> ModelRunOutcome:
        self.calls.append((tuple(spec.argv), timeout_seconds))
        return self.outcome


def floor_record(
    lines: Mapping[str, Sequence[str]],
    *,
    generated_at: datetime,
    not_ready: Mapping[str, str] | None = None,
    stopped: Sequence[Mapping[str, Any]] = (),
    lanes: Sequence[Mapping[str, Any]] = (),
) -> str:
    """A floor record: each line a list of task ids that are ``prep`` and ``ready`` (``not_ready``: key -> note)."""
    import json

    not_ready = not_ready or {}
    body = {
        "generated_at": generated_at.isoformat(),
        "lines": [
            {
                "line_id": line_id,
                "stations": [
                    {
                        "id": f"{line_id}-s1",
                        "items": [
                            {
                                "id": task,
                                "kind": "lane",
                                "state": "prep",
                                "note": not_ready.get(f"{line_id}:{task}", "ready"),
                                "prs": [],
                            }
                            for task in tasks
                        ],
                    }
                ],
            }
            for line_id, tasks in lines.items()
        ],
        "floor": {"lanes": list(lanes), "stopped": list(stopped)},
    }
    return json.dumps(body)


__all__: list[str] = [
    "ScriptedBriefs",
    "ScriptedClock",
    "ScriptedCommandForward",
    "ScriptedFacts",
    "ScriptedLedger",
    "ScriptedPool",
    "ScriptedStarter",
    "ScriptedTickets",
    "floor_record",
]
