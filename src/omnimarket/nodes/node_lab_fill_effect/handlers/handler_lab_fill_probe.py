# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Probe: the runner's pool read of every lab host, behind the idempotency precheck (OMN-20668).

Reads only. The run is already delivered when the ledger carries this fire's STATUS
row (unless ``force``); a ledger that cannot be read does not stop the run, the
same as the workflow's precheck. The pool read is the remote-lane runner's own, so
admission bars, lane slots and limited markers are the runner's, not copied here.
"""

from __future__ import annotations

from ..models import ModelLabFillProbeRequest, ModelLabFillProbeResult
from ..protocols import (
    LabFillPortError,
    ProtocolLabFillApprovedWork,
    ProtocolLabFillClock,
    ProtocolLabFillLedgerReader,
    ProtocolLabFillPlacementReader,
)
from .helpers_lab_fill_effect import default_clock, iso_utc


class HandlerLabFillProbe:
    """Read the pool, the approved-work depth and the precheck, and name what failed."""

    def __init__(
        self,
        placement: ProtocolLabFillPlacementReader | None = None,
        ledger: ProtocolLabFillLedgerReader | None = None,
        approved: ProtocolLabFillApprovedWork | None = None,
        clock: ProtocolLabFillClock | None = None,
    ) -> None:
        from ..protocols.local_lab_fill_adapters import (
            LocalApprovedWork,
            LocalLedgerReader,
            LocalPlacementReader,
        )

        self._placement = placement if placement is not None else LocalPlacementReader()
        self._ledger = ledger if ledger is not None else LocalLedgerReader()
        self._approved = approved if approved is not None else LocalApprovedWork()
        self._clock = default_clock(clock)

    def handle(self, request: ModelLabFillProbeRequest) -> ModelLabFillProbeResult:
        started = iso_utc(self._clock.now_epoch_s())
        notes: list[str] = []
        if not request.force:
            try:
                delivered = self._ledger.status_row_time(
                    request.ledger_path, request.run_key
                )
            except LabFillPortError as exc:
                delivered = None
                notes.append(f"precheck: {exc}")
            if delivered:
                return ModelLabFillProbeResult(
                    outcome="already-delivered",
                    precheck_evidence=delivered,
                    clock_utc=started,
                    finished_utc=iso_utc(self._clock.now_epoch_s()),
                    notes="; ".join(notes),
                )
        depth: int | None = None
        if request.approved_work_path:
            try:
                depth = self._approved.depth(request.approved_work_path)
            except LabFillPortError as exc:
                notes.append(f"approved-work: {exc}")
        try:
            readings = tuple(self._placement.read_pool())
            outcome = "read"
        except LabFillPortError as exc:
            readings = ()
            outcome = "unreadable"
            notes.append(f"pool: {exc}")
        return ModelLabFillProbeResult(
            outcome=outcome,
            readings=readings,
            approved_work_depth=depth,
            clock_utc=started,
            finished_utc=iso_utc(self._clock.now_epoch_s()),
            notes="; ".join(notes)[:500],
        )
