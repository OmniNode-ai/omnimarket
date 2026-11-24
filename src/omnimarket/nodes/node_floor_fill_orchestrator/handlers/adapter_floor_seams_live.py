# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live readers of the floor fill's four seams: pool, brake, stop signal and tickets.

Each reader answers from its source or raises ``FloorFillUnreadableError`` naming the source, so the pass prints
``UNKNOWN <source>-unread:<cause>`` and starts nothing. A reader never turns an unread source into an answer: an
absent, stale or empty pool snapshot is not zero free slots, and an unreadable stop queue is not an open brake.

* pool: the lane host pool snapshot the snapshot writer keeps (``pool.json`` and its ``.read-at.json`` record), read
  through the stamped pool, which refuses a missing or stale record.
* capacity brake: the same pool snapshot, read as the capacity verdicts of the hosts; fewer admitting hosts than the bound engages
  the brake (landing only). An unread or empty snapshot is never a released brake.
* brake and stop signal: the andon stop queue (stops opened by the detectors and by the ledger HOLD reader) through the
  andon stop gate (``stop_gate``). A stop of the class ``repository-without-lander`` that the gate refuses holds new lanes
  (landing only); a refusal by a stop of any other class is the stop signal.
* tickets: the floor-task ticket nodes (the payload compute, the entry-ticket check and the mint) over the Linear ticket
  store. The store is built on the first call that needs it, so a missing Linear credential is an unread tickets source on
  that call and not a failure of the whole pass.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)
from omnibase_spi.contracts.services import ModelLaneHostPoolRequest
from omnibase_spi.protocols.lane import ProtocolFloorTicketStore, ProtocolLaneHostPool
from pydantic import ValidationError

from omnimarket.models.floor_fill import (
    ModelFloorEntryTicketVerdict,
    ModelFloorTaskPayloadVerdict,
)
from omnimarket.models.floor_fill.model_floor_fill_candidates import (
    ModelFloorRecordRead,
)
from omnimarket.models.floor_fill.model_start_criterion import ModelFloorStartCriterion
from omnimarket.models.model_floor_task_ticket_effect import (
    ModelFloorTaskEntryTicketRequest,
    ModelFloorTaskTicketMintRequest,
)
from omnimarket.models.model_floor_task_ticket_payload import (
    ModelFloorTaskTicketPayloadRequest,
    ModelFloorTaskTicketPayloadResult,
)
from omnimarket.models.stop_gate import (
    EnumStopGateOutcome,
    ModelStopGateRequest,
    ModelStopPolicy,
    ModelStopRecord,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    FloorFillUnreadableError,
    ModelMintResult,
)
from omnimarket.nodes.node_floor_task_ticket_effect.handlers import (
    HandlerFloorTaskTicketEntryCheck,
    HandlerFloorTaskTicketMint,
)
from omnimarket.nodes.node_floor_task_ticket_payload_compute.handlers.handler_floor_task_ticket_payload import (
    HandlerFloorTaskTicketPayload,
)

#: The stop class (the andon registry's ``repository-without-lander``) whose refusal holds new lanes: landing is stuck.
BRAKE_STOP_CLASS = "repository-without-lander"
#: The lane name the stop gate is asked as: a floor fill lane holds no lease, so no stop exempts it.
GATE_LANE_ID = "floor-fill"

_HEADING = re.compile(r"^(?P<hashes>#{1,6})[ \t]+(?P<text>.*?)[ \t]*$")
_START_WHEN = re.compile(
    r"^[ \t]*(?:[-*][ \t]+)?\**Start when:\**[ \t]*(?P<rest>.*?)[ \t]*$",
    re.IGNORECASE,
)


def _cause(error: BaseException) -> str:
    """The type of the error, never its text: the text of a store or credential error may carry a secret."""
    if isinstance(error, FloorFillUnreadableError):
        return error.cause
    return type(error).__name__


class LiveFloorPool:
    """``ProtocolFloorPool`` over a lane host pool: the further lanes the hosts admit, never an unread pool as zero."""

    def __init__(self, pool: ProtocolLaneHostPool, engine: str) -> None:
        self._pool = pool
        self._engine = engine

    def free_slots(self) -> int:
        try:
            snapshot = self._pool.read_pool(
                ModelLaneHostPoolRequest(engine=self._engine)
            )
        except Exception as error:
            raise FloorFillUnreadableError(f"pool-snapshot:{error}") from error
        if not snapshot.readings:
            raise FloorFillUnreadableError("pool-snapshot:no-host-readings")
        return sum(
            reading.lane_slots
            for reading in snapshot.readings
            if reading.lane_admission_refusal is None
        )


class LiveFloorCapacityBrake:
    """The brake over the capacity the hosts advertise: engaged when fewer than ``min_admitting_hosts`` admit a further lane.

    Each reading's ``lane_admission_refusal`` is that host's capacity verdict (``node_host_capacity_compute``). It reads the
    pool the fill's own ``LiveFloorPool`` reads and refuses the same way: a snapshot that cannot be read, or holds no host
    reading, raises ``FloorFillUnreadableError`` so the pass answers ``UNKNOWN brake-unread:<cause>``.
    """

    def __init__(
        self, pool: ProtocolLaneHostPool, engine: str, min_admitting_hosts: int
    ) -> None:
        self._pool = pool
        self._engine = engine
        self._min_admitting_hosts = min_admitting_hosts

    def __call__(self) -> bool:
        try:
            snapshot = self._pool.read_pool(
                ModelLaneHostPoolRequest(engine=self._engine)
            )
        except Exception as error:
            raise FloorFillUnreadableError(f"pool-snapshot:{error}") from error
        if not snapshot.readings:
            raise FloorFillUnreadableError("pool-snapshot:no-host-readings")
        admitting = sum(
            1 for reading in snapshot.readings if reading.lane_admission_refusal is None
        )
        return admitting < self._min_admitting_hosts


def engaged_by_any(*brakes: Callable[[], bool]) -> Callable[[], bool]:
    """A brake engaged when any of ``brakes`` is: read in order, an engaged one answers and an unread one raises."""

    def engaged() -> bool:
        return any(brake() for brake in brakes)

    return engaged


@dataclass(frozen=True)
class LiveStopFacts:
    """What the stop gate is asked over: the open stops, the ``andon_stop`` policy, the gate's machine and the time."""

    stops: tuple[ModelStopRecord, ...]
    policy: ModelStopPolicy
    fsm: ModelFSMSubcontract
    now: datetime


class LiveFloorStopSignals:
    """The brake and the stop signal of the floor fill, both read through the andon stop gate."""

    def __init__(self, facts: Callable[[], LiveStopFacts]) -> None:
        self._facts = facts

    def _refusals(self) -> list[tuple[ModelStopRecord, str]]:
        from omnimarket.handlers.rules_stop_gate import stop_gate

        try:
            facts = self._facts()
            refusals: list[tuple[ModelStopRecord, str]] = []
            for stop in facts.stops:
                result = stop_gate(
                    ModelStopGateRequest(
                        scope=stop.scope,
                        stops=facts.stops,
                        policy=facts.policy,
                        now=facts.now,
                        lane_id=GATE_LANE_ID,
                    ),
                    facts.fsm,
                )
                if result.outcome is EnumStopGateOutcome.REFUSE:
                    refusals.append((stop, result.line))
        except Exception as error:
            raise FloorFillUnreadableError(f"andon-stops:{_cause(error)}") from error
        return refusals

    def brake(self) -> bool:
        """True while a stop of the landing class is open and refused by the gate: only landing proceeds."""
        return any(stop.cls == BRAKE_STOP_CLASS for stop, _line in self._refusals())

    def stop(self, task_class: str) -> str | None:
        """The gate's refusal text for the first refusing stop that is not the landing stop, else None."""
        for stop, line in self._refusals():
            if stop.cls != BRAKE_STOP_CLASS:
                return line
        return None


def _ready_tasks(floor_record: str) -> list[tuple[str, str]]:
    """``(line_id, task_id)`` of every task the record marks ready: a prep lane whose note starts ``ready``."""
    try:
        record = ModelFloorRecordRead.model_validate_json(floor_record)
    except (ValidationError, ValueError):
        return []
    return [
        (line.line_id, item.id)
        for line in record.lines
        for station in line.stations
        for item in station.items
        if item.state == "prep" and item.note.startswith("ready")
    ]


def _start_when(plan_text: str, task_id: str) -> str | None:
    """The ``Start when:`` line of the task's own section of the plan, labelled, or None when the section has none."""
    heading = re.compile(
        rf"^#{{1,6}}[ \t]+{re.escape(task_id)}(?:\.(?=[ \t]|$)[ \t]*|:[ \t]*|[ \t]+)"
    )
    lines = plan_text.splitlines()
    for index, line in enumerate(lines):
        if heading.match(line) is None:
            continue
        level = len(line) - len(line.lstrip("#"))
        for later in lines[index + 1 :]:
            next_heading = _HEADING.match(later)
            if next_heading is not None and len(next_heading["hashes"]) <= level:
                break
            found = _START_WHEN.match(later)
            if found is not None and found["rest"]:
                return f"Start when: {found['rest']}"
        return None
    return None


class LiveFloorTickets:
    """``ProtocolFloorTickets`` over the floor-task ticket nodes and one ticket store, built on first use."""

    def __init__(
        self,
        store: Callable[[], ProtocolFloorTicketStore],
        *,
        mint_timeout_seconds: float | None,
    ) -> None:
        self._store_factory = store
        self._mint_timeout_seconds = mint_timeout_seconds
        self._store: ProtocolFloorTicketStore | None = None
        self._plans: dict[str, str | None] = {}

    def _timeout(self) -> float:
        """The ticket store's deadline; an overlay that does not carry it leaves the tickets source unread."""
        if self._mint_timeout_seconds is None:
            raise FloorFillUnreadableError(
                "ticket-store:unconfigured:floor_fill_live.mint_timeout_seconds"
            )
        return self._mint_timeout_seconds

    def _ticket_store(self) -> ProtocolFloorTicketStore:
        self._timeout()
        if self._store is None:
            try:
                self._store = self._store_factory()
            except Exception as error:
                raise FloorFillUnreadableError(
                    f"ticket-store:{_cause(error)}"
                ) from error
        return self._store

    def _payload(
        self, line_id: str, task_id: str, plan_text: str | None
    ) -> ModelFloorTaskTicketPayloadResult:
        return HandlerFloorTaskTicketPayload().handle(
            ModelFloorTaskTicketPayloadRequest(
                line_id=line_id, task_id=task_id, plan_text=plan_text
            )
        )

    def payload_verdicts(
        self, floor_record: str, plan_texts: Mapping[str, str | None]
    ) -> Sequence[ModelFloorTaskPayloadVerdict]:
        self._plans = dict(plan_texts)
        verdicts: list[ModelFloorTaskPayloadVerdict] = []
        for line_id, task_id in _ready_tasks(floor_record):
            result = self._payload(line_id, task_id, plan_texts.get(line_id))
            verdicts.append(
                ModelFloorTaskPayloadVerdict(
                    line_id=line_id,
                    task_id=task_id,
                    ok=result.ok,
                    cause=result.cause,
                )
            )
        return verdicts

    def start_criteria(self, floor_record: str) -> Sequence[ModelFloorStartCriterion]:
        criteria: list[ModelFloorStartCriterion] = []
        for line_id, task_id in _ready_tasks(floor_record):
            plan_text = self._plans.get(line_id)
            start_when = None if plan_text is None else _start_when(plan_text, task_id)
            if start_when is not None:
                criteria.append(
                    ModelFloorStartCriterion(
                        line_id=line_id, task_id=task_id, start_when=start_when
                    )
                )
        return criteria

    def entry_ticket_verdicts(
        self, tickets: Mapping[str, str]
    ) -> Sequence[ModelFloorEntryTicketVerdict]:
        if not tickets:
            return []
        check = HandlerFloorTaskTicketEntryCheck(self._ticket_store())
        verdicts: list[ModelFloorEntryTicketVerdict] = []
        for key, ticket in tickets.items():
            _prefix, line_id, task_id = key.split(":", 2)
            read = check.handle(
                ModelFloorTaskEntryTicketRequest(
                    line_id=line_id,
                    task_id=task_id,
                    ticket=ticket,
                    mint_timeout_seconds=self._timeout(),
                )
            )
            verdicts.append(
                ModelFloorEntryTicketVerdict(
                    line_id=line_id,
                    task_id=task_id,
                    status=read.status,
                    ticket=read.ticket,
                    refusal=read.refusal,
                )
            )
        return verdicts

    def mint(self, key: str) -> ModelMintResult:
        _prefix, line_id, task_id = key.split(":", 2)
        plan_text = self._plans.get(line_id)
        if plan_text is None:
            return ModelMintResult(state="unavailable", cause="plan-unread")
        built = self._payload(line_id, task_id, plan_text)
        if built.payload is None:
            return ModelMintResult(state="refused", cause=built.cause)
        try:
            store = self._ticket_store()
        except FloorFillUnreadableError as error:
            return ModelMintResult(state="unavailable", cause=error.cause)
        minted = HandlerFloorTaskTicketMint(store).handle(
            ModelFloorTaskTicketMintRequest(
                floor_key=built.payload.floor_key,
                title=built.payload.title,
                description=built.payload.description,
                parent=built.payload.parent,
                mint_timeout_seconds=self._timeout(),
            )
        )
        return ModelMintResult(
            state=minted.state, ticket=minted.ticket, cause=minted.cause
        )


@dataclass(frozen=True)
class LiveSeamSources:
    """The reads behind the live seams, each replaceable by a test or another deployment; ``None`` takes the default."""

    ticket_store: Callable[[], ProtocolFloorTicketStore] | None = None
    stop_facts: Callable[[], LiveStopFacts] | None = None


def default_stop_facts(
    state_dir: Path,
    now: Callable[[], datetime],
    policy: ModelStopPolicy | None = None,
) -> Callable[[], LiveStopFacts]:
    """The open stops of the exception queue under ``state_dir`` (ledger HOLDs reconciled), the policy, the machine.

    ``policy`` is the stop policy the live settings supply; left ``None`` it is the ``andon_stop`` overlay's.
    """

    def read() -> LiveStopFacts:
        from omnimarket.handlers.rules_stop_gate import load_andon_stop_contract
        from omnimarket.models.exception_queue import ModelOpenStopsRequest
        from omnimarket.nodes.node_andon_stop_reducer.overlay import load_stop_policy
        from omnimarket.nodes.node_exception_queue_effect.container.container_exception_queue_effect import (
            create_handler_open_stops,
        )

        moment = now()
        opened = create_handler_open_stops().handle(
            ModelOpenStopsRequest(state_dir=state_dir, now=moment)
        )
        return LiveStopFacts(
            stops=opened.stops,
            policy=policy if policy is not None else load_stop_policy(),
            fsm=load_andon_stop_contract()[0],
            now=moment,
        )

    return read


def unconfigured_stop_facts(key: str) -> Callable[[], LiveStopFacts]:
    """Stop facts for an overlay without the stop queue directory: every read is unread, naming the absent key."""

    def read() -> LiveStopFacts:
        raise FloorFillUnreadableError(f"unconfigured:{key}")

    return read


__all__: list[str] = [
    "BRAKE_STOP_CLASS",
    "LiveFloorCapacityBrake",
    "LiveFloorPool",
    "LiveFloorStopSignals",
    "LiveFloorTickets",
    "LiveSeamSources",
    "LiveStopFacts",
    "default_stop_facts",
    "engaged_by_any",
    "unconfigured_stop_facts",
]
