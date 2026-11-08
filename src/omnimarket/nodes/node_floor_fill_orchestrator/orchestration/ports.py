# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams the orchestrator takes in its constructor, and the typed results they return.

Every seam is a narrow ``Protocol``. The fact source (``ProtocolFactSource``) and the process runner
(``ProtocolLaneProcessRunner``, one seam for the detached lane start and the bounded forward) are
omnibase_spi protocols and are re-exported here. The others are still declared here (the state store, the
pool, the ledger, the clock and the tickets seam, which carries T1's verdict models); each is bound to its
effect node by an adapter, and the golden tests run on the scripted ones in ``scripted_ports.py``.

A seam never raises for an expected outcome of the world: an unreadable source raises
``FloorFillUnreadableError`` with its cause, which the pass turns into ``UNKNOWN <source>-unread:<cause>``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from omnibase_spi.contracts.services import (
    ModelLaneProcessRunResult,
    ModelLaneProcessStartResult,
    ModelLaneStartSpec,
)
from omnibase_spi.protocols.lane import ProtocolFactSource, ProtocolLaneProcessRunner
from pydantic import BaseModel, ConfigDict

from omnimarket.models.floor_fill import (
    ModelFloorEntryTicketVerdict,
    ModelFloorTaskPayloadVerdict,
)

STATE_UNREACHABLE = "unreachable"

# The start spec carries no key, ticket or attempt of its own: the pass adds them to the environment of the
# detached start, so a runner (and the scripted one) can tell which key a start is for.
ENV_KEY = "FLOOR_FILL_KEY"
ENV_TICKET = "FLOOR_FILL_TICKET"
ENV_ATTEMPT = "FLOOR_FILL_ATTEMPT"
_MARKER_ENV = frozenset({ENV_KEY, ENV_TICKET, ENV_ATTEMPT})


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


class ModelMintResult(BaseModel):
    """The ticket mint for one key: a usable ticket, a refusal the line prints, or no answer in time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["ticket", "refused", "unavailable"]
    ticket: str | None = None
    cause: str | None = None


# The bounded foreground command and the detached start answer in omnibase_spi's own result models.
ModelRunOutcome = ModelLaneProcessRunResult


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
class ProtocolFloorClock(Protocol):
    def now(self) -> datetime: ...


def lane_start_from_spec(
    spec: ModelLaneStartSpec,
) -> ModelLaneStart | None:
    """Read a start spec back into the key, ticket and attempt the pass put in its environment (None: not a floor start)."""
    env = dict(spec.environment)
    if not env.keys() >= _MARKER_ENV:
        return None
    return ModelLaneStart(
        key=env[ENV_KEY],
        lane=spec.lane,
        ticket=env[ENV_TICKET],
        attempt=int(env[ENV_ATTEMPT]),
        argv=spec.argv,
        environment=tuple(kv for kv in spec.environment if kv[0] not in _MARKER_ENV),
        working_directory=spec.working_directory,
    )


__all__: list[str] = [
    "ENV_ATTEMPT",
    "ENV_KEY",
    "ENV_TICKET",
    "STATE_UNREACHABLE",
    "FloorFillUnreadableError",
    "ModelLaneProcessStartResult",
    "ModelLaneStart",
    "ModelLaneStartSpec",
    "ModelMintResult",
    "ModelRunOutcome",
    "ModelStateRead",
    "ProtocolFactSource",
    "ProtocolFloorClock",
    "ProtocolFloorLedger",
    "ProtocolFloorPool",
    "ProtocolFloorStateStore",
    "ProtocolFloorTickets",
    "ProtocolLaneProcessRunner",
    "lane_start_from_spec",
]
