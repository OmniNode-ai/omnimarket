# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The read surface of the check effect. Every method reads; none writes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from omnimarket.models.lab_job import ModelLabJobRow
from omnimarket.models.lab_job.model_lab_job_check import ModelLabJobPrObservation
from omnimarket.nodes.node_lab_job_check_effect.models import (
    ModelLaneActivityReading,
    ModelLedgerRowReading,
    ModelRelayReading,
)


class ProtocolLabJobCheckStore(Protocol):
    """Read access to the job table, the ledger projection, hook events and PR state."""

    async def open_jobs(self) -> tuple[ModelLabJobRow, ...]:
        """Every job outside ``done`` and ``alerted``."""
        ...

    async def claim_row(self, job: ModelLabJobRow) -> ModelLedgerRowReading | None:
        """The job's newest CLAIM row: by its ``run=`` cell, else by ticket and CLAIM time."""
        ...

    async def terminal_row_after(
        self, lane: str, claimed_at: datetime
    ) -> ModelLedgerRowReading | None:
        """The lane's newest TERMINAL row, only when newer than *claimed_at*."""
        ...

    async def lane_activity(
        self, claims: Mapping[str, datetime], until: datetime
    ) -> Mapping[str, ModelLaneActivityReading]:
        """Per lane, its lane-attributed hook events from its own CLAIM time until *until*."""
        ...

    async def relay(self, since: datetime, until: datetime) -> ModelRelayReading:
        """The newest hook event of any kind in ``[since, until)``, and how many."""
        ...

    async def pr_states(
        self, targets: Sequence[str]
    ) -> Mapping[str, ModelLabJobPrObservation]:
        """Last observed state of each ``owner/name#n`` target; absent when never observed."""
        ...


__all__: list[str] = ["ProtocolLabJobCheckStore"]
