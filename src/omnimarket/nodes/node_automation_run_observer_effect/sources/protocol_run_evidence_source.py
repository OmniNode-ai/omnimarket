# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam an evidence source plugs into.

A source reads one overlay entry's run facts from one kind of evidence and
returns the runs it found plus the cursor to resume from. The shard-1 sources
(receipts file, log line, launchd state) are registered by the handler; the
systemd journal, container, consumer-group and probe sources implement the same
protocol and are passed to the handler's constructor.
"""

from typing import Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, NonNegativeInt

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationEvidenceSource,
    EnumAutomationRunOutcome,
    ModelAutomationLivenessEntry,
    NonEmptyStr,
)
from omnimarket.nodes.node_automation_run_observer_effect.models.model_observer_state import (
    ModelProcessCursor,
)


class ModelObservedRun(BaseModel):
    """One run as the evidence shows it. finished_at is None while open."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: NonEmptyStr
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    outcome: EnumAutomationRunOutcome | None = None
    exit_code: int | None = None
    did_work_count: NonNegativeInt | None = None
    demand_count: NonNegativeInt | None = None
    unseen_runs: NonNegativeInt = 0
    evidence_ref: NonEmptyStr


class ModelEvidenceReadContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entry: ModelAutomationLivenessEntry
    cursor: ModelProcessCursor
    now: AwareDatetime
    launchd_domain: NonEmptyStr | None = None


class ModelEvidenceRead(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    runs: tuple[ModelObservedRun, ...] = ()
    cursor: ModelProcessCursor
    #: Why the evidence could not be read; a declared source that cannot be
    #: read is UNOBSERVABLE, never an empty read.
    unreadable: str | None = None
    #: Lines or records the source could not parse and skipped.
    malformed: NonNegativeInt = 0


class ProtocolRunEvidenceSource(Protocol):
    @property
    def source(self) -> EnumAutomationEvidenceSource: ...

    def read(self, context: ModelEvidenceReadContext) -> ModelEvidenceRead: ...
