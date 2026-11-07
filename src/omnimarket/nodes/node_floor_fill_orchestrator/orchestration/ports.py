# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams the orchestrator takes in its constructor, and the typed results they return.

Every seam is a narrow ``Protocol``. ``ProtocolWorkLedgerStore`` and ``ProtocolLaneHostPool`` live in
omnibase_spi; the others are declared here until their omnibase_spi protocols merge (the fact source
of this task, the process runner of T2 and the ticket store of T11): each is bound to its effect node
by an adapter, and the golden tests run on the scripted ones in ``scripted_ports.py``.

A seam never raises for an expected outcome of the world: an unreadable source raises
``FloorFillUnreadableError`` with its cause, which the pass turns into ``UNKNOWN <source>-unread:<cause>``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_floor_fill_candidates_compute.handlers import (
    ModelFloorEntryTicketVerdict,
    ModelFloorTaskPayloadVerdict,
)

STATE_UNREACHABLE = "unreachable"


class FloorFillUnreadableError(Exception):
    """A source or the store could not be read; ``cause`` is printed after the colon."""

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


class ModelStateRead(BaseModel):
    """One row as stored: its payload (None for no row) and its version (0 for no row)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    payload: str | None
    version: int


class ModelLaneStart(BaseModel):
    """What the process effect is asked to start: the argv is ``floor_fill.runner_command`` filled in."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    lane: str
    ticket: str
    attempt: int
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    working_directory: str


class ModelLaneStartOutcome(BaseModel):
    """``started`` or ``failed(cause)``: the process effect's answer. A start is confirmed by the ledger, never by this."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["started", "failed"]
    cause: str | None = None


class ModelMintResult(BaseModel):
    """The ticket mint for one key: a usable ticket, a refusal the line prints, or no answer in time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["ticket", "refused", "unavailable"]
    ticket: str | None = None
    cause: str | None = None


class ModelRunOutcome(BaseModel):
    """A bounded foreground command: ``exited`` with a code and output, ``timed_out`` or ``failed``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["exited", "timed_out", "failed"]
    exit_code: int | None = None
    stdout: str = ""
    cause: str | None = None


@runtime_checkable
class ProtocolFloorStateStore(Protocol):
    """The state rows under a conditional write: a write lands only if the stored version is the expected one."""

    def load(self, key: str) -> ModelStateRead:
        """Read one row; raises ``FloorFillUnreadableError`` when the store cannot be reached or read."""
        ...

    def compare_and_set(
        self, key: str, expected_version: int, new_version: int, payload: str
    ) -> bool:
        """Write ``payload`` as ``new_version`` iff the stored version is ``expected_version`` (0: no row)."""
        ...


@runtime_checkable
class ProtocolFloorFactSource(Protocol):
    """The facts a pass reads: the floor, the plan texts, the open-count brake and the stop signals."""

    def read_floor(self, url: str) -> str: ...

    def read_plan(self, plans_root: str, line_id: str) -> str: ...

    def landing_only(self) -> bool:
        """True while the open-count brake holds (the open count rose on two reads)."""
        ...

    def stop_refuses(self, task_class: str) -> str | None:
        """The cause when a stop signal refuses the class, else None."""
        ...


@runtime_checkable
class ProtocolFloorPool(Protocol):
    """The free lane slots across the pool; bound to ``ProtocolLaneHostPool.read_pool``."""

    def free_slots(self) -> int: ...


@runtime_checkable
class ProtocolFloorLedger(Protocol):
    """The ledger reads of the pass; bound to ``ProtocolWorkLedgerStore.rows_for_lane`` and the lane-liveness read."""

    def claim_found(self, lane: str, key: str) -> bool:
        """True when a CLAIM row carries the lane name and the ``key=`` cell, open or closed."""
        ...

    def lane_ended(self, lane: str) -> bool:
        """True when the lane is terminal or dead by the ledger's lane-liveness read."""
        ...


@runtime_checkable
class ProtocolFloorTickets(Protocol):
    """T11's compute and effect behind one seam: payload verdicts, entry-ticket reads, the mint."""

    def payload_verdicts(
        self, floor_record: str, plan_texts: Mapping[str, str | None]
    ) -> Sequence[ModelFloorTaskPayloadVerdict]:
        """Pure: one verdict per ready task of the record. Makes no Linear call."""
        ...

    def entry_ticket_verdicts(
        self, tickets: Mapping[str, str]
    ) -> Sequence[ModelFloorEntryTicketVerdict]:
        """Read each ``{key: ticket}``; one Linear read per entry that holds a ticket."""
        ...

    def mint(self, key: str) -> ModelMintResult:
        """Search by ``Floor key:`` then create; fail closed (``unavailable``) past the deadline."""
        ...


@runtime_checkable
class ProtocolFloorLaneStarter(Protocol):
    """T2's ``start_detached`` behind one seam."""

    def start(self, start: ModelLaneStart) -> ModelLaneStartOutcome: ...


@runtime_checkable
class ProtocolFloorCommandRunner(Protocol):
    """T2's ``run_bounded``: run a command in the foreground under a deadline (the forward to the fill host)."""

    def run_bounded(
        self, argv: Sequence[str], timeout_seconds: float
    ) -> ModelRunOutcome: ...


@runtime_checkable
class ProtocolFloorClock(Protocol):
    def now(self) -> datetime: ...


__all__: list[str] = [
    "STATE_UNREACHABLE",
    "FloorFillUnreadableError",
    "ModelLaneStart",
    "ModelLaneStartOutcome",
    "ModelMintResult",
    "ModelRunOutcome",
    "ModelStateRead",
    "ProtocolFloorClock",
    "ProtocolFloorCommandRunner",
    "ProtocolFloorFactSource",
    "ProtocolFloorLaneStarter",
    "ProtocolFloorLedger",
    "ProtocolFloorPool",
    "ProtocolFloorStateStore",
    "ProtocolFloorTickets",
]
