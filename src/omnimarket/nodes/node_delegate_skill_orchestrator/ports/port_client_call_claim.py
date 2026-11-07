# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Claim store for one client call: one row, fenced writes, leases (OMN-21098).

Plan task TB.3. A client call is one model-raised ``Agent`` call that the
Claude Code adapter offers to delegation. Its row makes the call dispatch at
most once, whatever crashes, redeliveries and lease expiries happen around it.
The rules are those of the AgentCallAdapter TLA+ model
(``formal/agent_call_adapter``), whose ten one-knob mutants each break one of
them:

* the claim inserts only when the row is absent and never resets a live row;
* a second command on the same key with a different fingerprint is a conflict
  and writes nothing;
* a stored terminal is republished to a later command on the same key;
* a live lease means the owner is still working, so the caller waits;
* an expired lease is taken over, which raises the generation, and the
  takeover resumes from the recorded state;
* every write after the claim is guarded on the writer's generation, so a stale
  owner changes zero rows;
* a row that recorded its dispatch intent is never dispatched again.

The lease length and the clock are injected: no value is chosen here. The
in-memory store is the default backend; the Postgres-backed store and its
migration land in the following slice of TB.3 and implement this same
protocol.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_delegate_skill_orchestrator.models.enum_client_call_claim_action import (
    EnumClientCallClaimAction,
    EnumClientCallResumeStep,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.enum_client_call_claim_state import (
    EnumClientCallClaimState,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_client_call_claim_decision import (
    ModelClientCallClaimDecision,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_client_call_claim_row import (
    ModelClientCallClaimRow,
)

_S = EnumClientCallClaimState

# The writes a row accepts, by the state it is in. An admission commit leaves
# CLAIMED for one of three states; only ADMITTED may record a dispatch intent;
# the terminal is stored from any state that has finished its admission.
_ALLOWED_TRANSITIONS: dict[
    EnumClientCallClaimState, frozenset[EnumClientCallClaimState]
] = {
    _S.CLAIMED: frozenset({_S.ADMITTED, _S.NOT_ADMITTED, _S.OBSERVED}),
    _S.ADMITTED: frozenset({_S.DISPATCH_INTENDED}),
    _S.NOT_ADMITTED: frozenset({_S.TERMINAL}),
    _S.OBSERVED: frozenset({_S.TERMINAL}),
    _S.DISPATCH_INTENDED: frozenset({_S.TERMINAL}),
    _S.TERMINAL: frozenset(),
}

_RESUME_STEPS: dict[EnumClientCallClaimState, EnumClientCallResumeStep] = {
    _S.CLAIMED: EnumClientCallResumeStep.ADMIT,
    _S.ADMITTED: EnumClientCallResumeStep.PUBLISH_ADMISSION,
    _S.NOT_ADMITTED: EnumClientCallResumeStep.PUBLISH_ADMISSION,
    _S.OBSERVED: EnumClientCallResumeStep.PUBLISH_ADMISSION,
    _S.DISPATCH_INTENDED: EnumClientCallResumeStep.AWAIT_TERMINAL,
}


@runtime_checkable
class ProtocolClientCallClaimStore(Protocol):
    """Claim a client call and move its row, fenced by generation."""

    def claim(
        self, *, client_call_key: str, request_fingerprint: str, owner_id: str
    ) -> ModelClientCallClaimDecision:
        """Insert the row, or answer from the row already there."""
        ...

    def advance(
        self,
        *,
        client_call_key: str,
        generation: int,
        to_state: EnumClientCallClaimState,
        effective_mode: str | None = None,
        admitted: bool | None = None,
        terminal_json: str = "",
    ) -> bool:
        """Guarded write: True when it changed the row, False when it changed nothing."""
        ...

    def read(self, client_call_key: str) -> ModelClientCallClaimRow | None:
        """The row for a key, or None."""
        ...

    def stalled(self) -> list[ModelClientCallClaimRow]:
        """Non-terminal rows whose lease has expired, for the reconciler."""
        ...


class InMemoryClientCallClaimStore:
    """The default backend: a lock-guarded dict, with an injected clock and lease."""

    def __init__(self, *, clock: Callable[[], datetime], lease_seconds: float) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._clock = clock
        self._lease = timedelta(seconds=lease_seconds)
        self._rows: dict[str, ModelClientCallClaimRow] = {}
        self._lock = threading.Lock()

    # -- the three decisions the TLA+ mutants flip, each its own method -------

    def _writable(self, row: ModelClientCallClaimRow, generation: int) -> bool:
        """A write lands only when the writer still holds the row's generation."""
        return row.generation == generation

    def _resume_step(self, state: EnumClientCallClaimState) -> EnumClientCallResumeStep:
        """Where a takeover resumes; a recorded dispatch intent resumes at the wait."""
        return _RESUME_STEPS[state]

    def _claim_existing(
        self,
        row: ModelClientCallClaimRow,
        request_fingerprint: str,
        owner_id: str,
        now: datetime,
    ) -> ModelClientCallClaimDecision:
        if row.request_fingerprint != request_fingerprint:
            return ModelClientCallClaimDecision(
                action=EnumClientCallClaimAction.CONFLICT, row=row
            )
        if row.state is _S.TERMINAL:
            return ModelClientCallClaimDecision(
                action=EnumClientCallClaimAction.REPUBLISH_TERMINAL, row=row
            )
        if row.lease_expires_at > now:
            return ModelClientCallClaimDecision(
                action=EnumClientCallClaimAction.WAIT_LIVE_OWNER, row=row
            )
        taken = row.model_copy(
            update={
                "generation": row.generation + 1,
                "owner_id": owner_id,
                "lease_expires_at": now + self._lease,
            }
        )
        self._rows[row.client_call_key] = taken
        return ModelClientCallClaimDecision(
            action=EnumClientCallClaimAction.TAKEOVER,
            row=taken,
            resume_step=self._resume_step(row.state),
        )

    # -- the protocol ---------------------------------------------------------

    def claim(
        self, *, client_call_key: str, request_fingerprint: str, owner_id: str
    ) -> ModelClientCallClaimDecision:
        now = self._clock()
        with self._lock:
            row = self._rows.get(client_call_key)
            if row is not None:
                return self._claim_existing(row, request_fingerprint, owner_id, now)
            fresh = ModelClientCallClaimRow(
                client_call_key=client_call_key,
                request_fingerprint=request_fingerprint,
                state=_S.CLAIMED,
                generation=1,
                owner_id=owner_id,
                lease_expires_at=now + self._lease,
            )
            self._rows[client_call_key] = fresh
            return ModelClientCallClaimDecision(
                action=EnumClientCallClaimAction.WON, row=fresh
            )

    def advance(
        self,
        *,
        client_call_key: str,
        generation: int,
        to_state: EnumClientCallClaimState,
        effective_mode: str | None = None,
        admitted: bool | None = None,
        terminal_json: str = "",
    ) -> bool:
        now = self._clock()
        with self._lock:
            row = self._rows.get(client_call_key)
            if row is None or not self._writable(row, generation):
                return False
            if to_state not in _ALLOWED_TRANSITIONS[row.state]:
                return False
            update: dict[str, object] = {
                "state": to_state,
                "lease_expires_at": now + self._lease,
            }
            if effective_mode is not None:
                update["effective_mode"] = effective_mode
            if admitted is not None:
                update["admitted"] = admitted
            if to_state is _S.TERMINAL:
                update["terminal_json"] = terminal_json
            self._rows[client_call_key] = row.model_copy(update=update)
            return True

    def read(self, client_call_key: str) -> ModelClientCallClaimRow | None:
        with self._lock:
            return self._rows.get(client_call_key)

    def stalled(self) -> list[ModelClientCallClaimRow]:
        now = self._clock()
        with self._lock:
            return [
                row
                for row in self._rows.values()
                if row.state is not _S.TERMINAL and row.lease_expires_at <= now
            ]
