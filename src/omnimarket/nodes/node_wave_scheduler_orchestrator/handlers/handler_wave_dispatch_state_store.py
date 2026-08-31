# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""State-store wave dispatcher — the wave scheduler's effect boundary [OMN-17017].

``ProtocolWaveDispatcher`` had no implementation anywhere in the repo, so every
live wave-scheduler run raised
``RuntimeError("dispatcher adapter required when dry_run is false")`` — and a
*passing* test asserted that failure as expected behaviour. The planner shipped;
the executor never did (2026-08-29 beta off-the-rails analysis rev 2, §RC-J).

Substrate
---------
Durable dispatch-lifecycle records under ``$ONEX_STATE_DIR`` — no daemon, no new
runner, no bus producer invented for the occasion. Per ticket, per wave:

1. append a ``dispatch_requested`` record to
   ``<state>/wave_scheduler/<run_id>/lifecycle.ndjson``;
2. read the terminal outcome back from
   ``<state>/wave_scheduler/<run_id>/observed/<ticket_id>.json``;
3. append ``observed`` (with the status) or ``unreported``.

A ticket with no outcome file is **not** reported as a status. It comes back
missing from the status map, the orchestrator marks it UNREPORTED, and it stays
visibly pending — "requested" is never a proxy for "executed". The record shape
is the one OMN-16176's obligation projection consumes; when that projection
lands, this writer is the producer and the NDJSON becomes a replay source rather
than the authority.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_wave_scheduler_orchestrator.models.model_wave_scheduler_result import (
    EnumDispatchLifecyclePhase,
    EnumTicketExecutionStatus,
    ModelDispatchLifecycleRecord,
    ModelObservedTicketOutcome,
    ModelWaveAssignment,
)
from omnimarket.nodes.node_wave_scheduler_orchestrator.wave_state import run_dir


class HandlerWaveDispatchStateStore:
    """Records dispatch obligations and reads observed outcomes back."""

    def __init__(
        self,
        *,
        state_dir: Path,
        run_id: str,
        now_utc: datetime | None = None,
    ) -> None:
        self._run_dir = run_dir(state_dir, run_id)
        self._run_id = run_id
        self._now_utc = now_utc

    @property
    def lifecycle_path(self) -> Path:
        return self._run_dir / "lifecycle.ndjson"

    def dispatch_wave(
        self, assignment: ModelWaveAssignment
    ) -> Mapping[str, EnumTicketExecutionStatus]:
        repos = dict(assignment.repo_assignments)
        statuses: dict[str, EnumTicketExecutionStatus] = {}
        for ticket_id in assignment.ticket_ids:
            repo = repos.get(ticket_id, "")
            self._append(
                assignment.wave_id,
                ticket_id,
                repo,
                EnumDispatchLifecyclePhase.DISPATCH_REQUESTED,
                None,
            )
            outcome = self._read_observed(ticket_id)
            if outcome is None:
                self._append(
                    assignment.wave_id,
                    ticket_id,
                    repo,
                    EnumDispatchLifecyclePhase.UNREPORTED,
                    None,
                )
                continue
            statuses[ticket_id] = outcome.status
            self._append(
                assignment.wave_id,
                ticket_id,
                repo,
                EnumDispatchLifecyclePhase.OBSERVED,
                outcome.status,
            )
        return statuses

    def _read_observed(self, ticket_id: str) -> ModelObservedTicketOutcome | None:
        path = self._run_dir / "observed" / f"{ticket_id}.json"
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        status = raw.get("status")
        if status not in set(EnumTicketExecutionStatus):
            raise ValueError(
                f"observed outcome for {ticket_id} carries unknown status {status!r} "
                f"(file: {path})"
            )
        return ModelObservedTicketOutcome.model_validate(raw)

    def _append(
        self,
        wave_id: int,
        ticket_id: str,
        repo: str,
        phase: EnumDispatchLifecyclePhase,
        status: EnumTicketExecutionStatus | None,
    ) -> None:
        record = ModelDispatchLifecycleRecord(
            run_id=self._run_id,
            wave_id=wave_id,
            ticket_id=ticket_id,
            repo=repo,
            phase=phase,
            status=status,
            recorded_at_utc=self._timestamp(),
        )
        self.lifecycle_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lifecycle_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.model_dump(mode="json")) + "\n")

    def _timestamp(self) -> str:
        now = self._now_utc or datetime.now(UTC)
        return now.isoformat().replace("+00:00", "Z")


__all__ = ["HandlerWaveDispatchStateStore"]
