# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pinned inputs and the real-node binding of the recorded-floor replay (plan task T8).

Every input is pinned: the 10:40Z floor record (two variants), the plan texts, the initial state row, the overlay,
a fixed clock and the ticket store's numbering. The orchestrator runs on the real nodes behind its seams: T1's
candidates compute, T12's reducer, T4's lines compute, T11's payload compute and mint effect over T11's in-memory
ticket store, and T2's scripted runner. Nothing is scripted that a node decides.

A scripted binding fails the test on a call that is not in the script: ``StrictFacts`` records its calls and the
replay compares them with the script; ``AdapterScriptedRunner`` raises when it has nothing scripted for a call.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnibase_spi.contracts.services import (
    ModelLaneProcessStartResult,
    ModelLaneStartSpec,
)
from omnimarket.nodes.node_floor_fill_orchestrator.handlers.handler_floor_fill_command import (
    HandlerFloorFillCommand,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    ModelFloorFillConfig,
    load_floor_fill_config,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.core import (
    FloorFillOrchestrator,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    FloorFillUnreadableError,
    ModelMintResult,
    ModelRunOutcome,
    lane_start_from_spec,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.scripted_ports import (
    ScriptedClock,
    ScriptedCommandForward,
    ScriptedLedger,
    ScriptedPool,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.store import (
    InMemoryFloorStateStore,
)
from omnimarket.nodes.node_floor_task_ticket_effect.adapters.adapter_inmemory_floor_ticket_store import (
    InMemoryFloorTicketStore,
)
from omnimarket.nodes.node_floor_task_ticket_effect.handlers import (
    HandlerFloorTaskTicketEntryCheck,
    HandlerFloorTaskTicketMint,
)
from omnimarket.nodes.node_floor_task_ticket_effect.models import (
    ModelFloorTaskEntryTicketRequest,
    ModelFloorTaskTicketMintRequest,
)
from omnimarket.nodes.node_floor_task_ticket_payload_compute.handlers.handler_floor_task_ticket_payload import (
    HandlerFloorTaskTicketPayload,
)
from omnimarket.nodes.node_floor_task_ticket_payload_compute.models import (
    ModelFloorTaskTicketPayload,
    ModelFloorTaskTicketPayloadRequest,
)
from omnimarket.nodes.node_lane_process_effect.adapters import AdapterScriptedRunner
from pydantic import ValidationError

from omnimarket.nodes.node_floor_fill_candidates_compute.handlers import (
    ModelFloorEntryTicketVerdict,
    ModelFloorRecordRead,
    ModelFloorTaskPayloadVerdict,
)
from omnimarket.nodes.node_floor_fill_candidates_compute.handlers.handler_floor_fill_candidates import (
    HandlerFloorFillCandidates,
)
from omnimarket.nodes.node_floor_fill_lines_compute.handlers import (
    HandlerFloorFillLines,
)
from omnimarket.nodes.node_floor_fill_reducer.handlers import HandlerFloorFillReducer

FIXTURES = Path(__file__).parent / "fixtures"
VARIANT_A_PATH = FIXTURES / "factory-floor-2026-11-02T1040Z.variant-a.json"
VARIANT_B_PATH = FIXTURES / "factory-floor-2026-11-02T1040Z.variant-b.json"
PLANS_DIR = FIXTURES / "plans"

# The tick runs one minute after the floor record was generated.
NOW = datetime(2026, 11, 2, 10, 41, tzinfo=UTC)
FREE_SLOTS = 20
SCOPE = "floor-fill-replay"
HOST = "fill-host"
MINT_TIMEOUT_SECONDS = 5.0
# The scripted ticket store numbers its tickets from here; no live ticket has these numbers.
TICKET_PREFIX = "OMN"
TICKET_FIRST_NUMBER = 900001

OVERLAY: dict[str, Any] = {
    "scope": SCOPE,
    "host_id": HOST,
    "url": "scripted://floor",
    "plans_root": "scripted://plans",
    "runner_command": [
        "node-entry",
        "--lane",
        "{lane}",
        "--key",
        "{key}",
        "--ticket",
        "{ticket}",
        "--attempt",
        "{attempt}",
    ],
    "reserved_timeout_seconds": 600,
    "sent_window_minutes": 120,
    "command_timeout_seconds": 60,
    "tick_interval_seconds": 300,
    "remote_argv": ["remote-shell", "{argv}"],
    "process_environment": {"EXAMPLE": "1"},
    "process_working_directory": "/scripted/work",
    "max_age_minutes": 15,
    "max_fill_lanes_per_line": 1,
}


def overlay(**changes: Any) -> dict[str, Any]:
    return {**OVERLAY, **changes}


def floor_text(variant: str) -> str:
    """The recorded floor record of a variant (``a``: as recorded, ``b``: the operator's shape), exact bytes."""
    path = {"a": VARIANT_A_PATH, "b": VARIANT_B_PATH}[variant]
    return path.read_text(encoding="utf-8")


def plan_texts() -> dict[str, str]:
    """The plan text of every line of the record, by line id."""
    return {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted(PLANS_DIR.glob("*.md"))
    }


class StrictFacts:
    """The floor, plan texts, brake and stop signal as recorded; every call is kept, any other source is refused."""

    synchronous_execution = True

    def __init__(self, floor: str, plans: Mapping[str, str]) -> None:
        self._floor = floor
        self._plans = dict(plans)
        self.calls: list[str] = []

    def read_floor(self, url: str) -> str:
        self.calls.append(f"floor:{url}")
        return self._floor

    def read_plan(self, plans_root: str, line_id: str) -> str:
        self.calls.append(f"plan:{plans_root}:{line_id}")
        if line_id not in self._plans:
            raise FloorFillUnreadableError("plan-absent")
        return self._plans[line_id]

    def landing_only(self) -> bool:
        self.calls.append("brake")
        return False

    def stop_refuses(self, task_class: str) -> str | None:
        self.calls.append("stop-signal")
        return None


class RealTickets:
    """T11's compute and effect behind the orchestrator's tickets seam, over T11's in-memory ticket store."""

    def __init__(self, store: InMemoryFloorTicketStore) -> None:
        self.store = store
        self.calls: list[str] = []
        self._payloads: dict[str, ModelFloorTaskTicketPayload] = {}

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
                    result = HandlerFloorTaskTicketPayload().handle(
                        ModelFloorTaskTicketPayloadRequest(
                            line_id=line.line_id,
                            task_id=item.id,
                            plan_text=plan_texts.get(line.line_id),
                        )
                    )
                    if result.payload is not None:
                        self._payloads[result.payload.floor_key] = result.payload
                    out.append(
                        ModelFloorTaskPayloadVerdict(
                            line_id=line.line_id,
                            task_id=item.id,
                            ok=result.ok,
                            cause=result.cause,
                        )
                    )
        return out

    def entry_ticket_verdicts(
        self, tickets: Mapping[str, str]
    ) -> Sequence[ModelFloorEntryTicketVerdict]:
        self.calls.append("ticket-read")
        check = HandlerFloorTaskTicketEntryCheck(self.store)
        out: list[ModelFloorEntryTicketVerdict] = []
        for key, ticket in tickets.items():
            _, line_id, task_id = key.split(":", 2)
            verdict = check.handle(
                ModelFloorTaskEntryTicketRequest(
                    line_id=line_id,
                    task_id=task_id,
                    ticket=ticket,
                    mint_timeout_seconds=MINT_TIMEOUT_SECONDS,
                )
            )
            if verdict.status == "usable":
                out.append(
                    ModelFloorEntryTicketVerdict(
                        line_id=line_id,
                        task_id=task_id,
                        status="usable",
                        ticket=verdict.ticket,
                    )
                )
            elif verdict.status == "refused":
                out.append(
                    ModelFloorEntryTicketVerdict(
                        line_id=line_id,
                        task_id=task_id,
                        status="refused",
                        refusal=verdict.refusal,
                    )
                )
            else:
                out.append(
                    ModelFloorEntryTicketVerdict(
                        line_id=line_id,
                        task_id=task_id,
                        status="unreadable",
                        cause=verdict.cause,
                    )
                )
        return out

    def mint(self, key: str) -> ModelMintResult:
        self.calls.append(f"mint:{key}")
        payload = self._payloads.get(key)
        if payload is None:
            return ModelMintResult(state="refused", cause="no-payload")
        result = HandlerFloorTaskTicketMint(self.store).handle(
            ModelFloorTaskTicketMintRequest(
                **payload.model_dump(), mint_timeout_seconds=MINT_TIMEOUT_SECONDS
            )
        )
        return ModelMintResult(
            state=result.state, ticket=result.ticket, cause=result.cause
        )


class ClaimingRunner(AdapterScriptedRunner):
    """T2's scripted runner; a start it answers ``started`` for is what the lane's leader would CLAIM.

    The ledger is told of the CLAIM only when the script says the start succeeded, so a refused start leaves no row.
    """

    def __init__(self, ledger: ScriptedLedger, **script: Any) -> None:
        super().__init__(**script)
        self._ledger = ledger

    def start_detached(self, spec: ModelLaneStartSpec) -> ModelLaneProcessStartResult:
        result = super().start_detached(spec)
        start = lane_start_from_spec(spec)
        if result.state == "started" and start is not None:
            self._ledger.claim(start.lane, start.key)
        return result


def working_runner(ledger: ScriptedLedger, starts: int) -> ClaimingRunner:
    """A runner scripted to start exactly ``starts`` lanes; one call more raises."""
    return ClaimingRunner(
        ledger,
        starts=tuple(
            ModelLaneProcessStartResult(state="started", pid=1000 + n)
            for n in range(starts)
        ),
    )


def refusing_runner(ledger: ScriptedLedger, refusals: int) -> ClaimingRunner:
    """A runner scripted to refuse exactly ``refusals`` starts; one call more raises."""
    return ClaimingRunner(
        ledger,
        starts=tuple(
            ModelLaneProcessStartResult(state="failed", cause="exit-3")
            for _ in range(refusals)
        ),
    )


@dataclass
class Replay:
    """One replay: every seam kept so the test can count and compare what each did."""

    config: ModelFloorFillConfig
    store: InMemoryFloorStateStore
    facts: StrictFacts
    pool: ScriptedPool
    ledger: ScriptedLedger
    ticket_store: InMemoryFloorTicketStore
    tickets: RealTickets
    runner: ClaimingRunner
    clock: ScriptedClock
    forward: ScriptedCommandForward = field(
        default_factory=lambda: ScriptedCommandForward(
            ModelRunOutcome(state="failed", cause="no forward in this replay")
        )
    )

    def orchestrator(self) -> FloorFillOrchestrator:
        return FloorFillOrchestrator(
            config=self.config,
            store=self.store,
            facts=self.facts,
            pool=self.pool,
            ledger=self.ledger,
            tickets=self.tickets,
            runner=self.runner,
            clock=self.clock,
            candidates=HandlerFloorFillCandidates(),
            reducer=HandlerFloorFillReducer(),
            lines=HandlerFloorFillLines(),
        )

    def handler(self) -> HandlerFloorFillCommand:
        """The entry's handler on this replay's seams: what the unit's ``tick`` and the ``lines`` entry run."""
        return HandlerFloorFillCommand(
            config=self.config, orchestrator=self.orchestrator(), runner=self.forward
        )

    def rows(self) -> dict[str, dict[str, Any]]:
        """The state rows as stored (payload and version), by key, for a byte-for-byte comparison."""
        out: dict[str, dict[str, Any]] = {}
        for key in (SCOPE, SCOPE + ":answer"):
            read = self.store.load(key)
            out[key] = {
                "version": read.version,
                "payload": None if read.payload is None else json.loads(read.payload),
            }
        return out


def key_safe_inputs(variant: str) -> tuple[str, dict[str, str]]:
    """The same record and plans with every ``.`` of a task id written ``-``: ids the reducer's key rule admits.

    Used only as the control that shows what else holds when the recorded ids are not the blocker.
    """
    record = json.loads(floor_text(variant))
    dotted: list[str] = []
    for line in record["lines"]:
        for station in line["stations"]:
            for item in station["items"]:
                if "." in item["id"]:
                    dotted.append(item["id"])
                    item["id"] = item["id"].replace(".", "-")
    plans = plan_texts()
    for line_id, text in plans.items():
        for task_id in dotted:
            text = text.replace(task_id, task_id.replace(".", "-"))
        plans[line_id] = text
    return json.dumps(record, indent=2) + "\n", plans


def replay(
    *,
    variant: str,
    runner: str,
    ids: str = "recorded",
    overlay_changes: Mapping[str, Any] | None = None,
    starts: int | None = None,
) -> Replay:
    """Build one replay of a variant, from the same initial state every time.

    ``runner`` is ``working`` or ``refusing``; ``starts`` is how many starts the script holds for it (default: one per
    ready line of the variant). ``ids`` is ``recorded`` (the task ids as the floor records them) or ``key-safe``.
    """
    expected = {"a": 6, "b": 4}[variant] if starts is None else starts
    ledger = ScriptedLedger()
    ticket_store = InMemoryFloorTicketStore(
        prefix=TICKET_PREFIX, first_number=TICKET_FIRST_NUMBER
    )
    script = working_runner if runner == "working" else refusing_runner
    return Replay(
        config=load_floor_fill_config(overlay(**dict(overlay_changes or {}))),
        store=InMemoryFloorStateStore(),
        facts=StrictFacts(
            *(
                (floor_text(variant), plan_texts())
                if ids == "recorded"
                else key_safe_inputs(variant)
            )
        ),
        pool=ScriptedPool(FREE_SLOTS),
        ledger=ledger,
        ticket_store=ticket_store,
        tickets=RealTickets(ticket_store),
        runner=script(ledger, expected),
        clock=ScriptedClock(NOW),
    )


__all__: list[str] = [
    "FREE_SLOTS",
    "HOST",
    "NOW",
    "OVERLAY",
    "SCOPE",
    "ClaimingRunner",
    "RealTickets",
    "Replay",
    "StrictFacts",
    "floor_text",
    "key_safe_inputs",
    "overlay",
    "plan_texts",
    "refusing_runner",
    "replay",
    "working_runner",
]
