# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the beta board (OMN-23315): the overlay that declares it, the readings it is computed from, and the board it writes.

Nothing here names a criterion, a repository or a workflow. The overlay declares them; the contract's runtime block declares the
paths, names and time limits.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
    ModelFileLedgerReadRoute,
)

ReadingVerdict = Literal["pass", "fail", "unknown"]
SourceKind = Literal["workflow_run", "ledger_row"]
SourceState = Literal["pass", "fail", "stale", "unread"]
CriterionState = Literal["pass", "fail", "stale", "unread", "no_reading"]


class ModelBetaCarrier(BaseModel):
    """A ticket whose work carries a criterion toward its proof."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket: str = Field(min_length=1)


class ModelBetaProbe(BaseModel):
    """A workflow whose scheduled runs are one reading of a criterion: success is a pass, failure is a fail."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    workflow: str = Field(min_length=1)
    max_age_seconds: int = Field(ge=1)
    # The events whose runs count as readings; an empty tuple counts every event.
    events: tuple[str, ...] = ()


class ModelBetaLedgerReading(BaseModel):
    """A reading carried by ledger rows: the rows of one type whose cells match, each saying its verdict in one cell."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    row_type: str = Field(min_length=1)
    cells: dict[str, str] = Field(default_factory=dict)
    verdict_cell: str = Field(min_length=1)
    pass_values: tuple[str, ...] = Field(min_length=1)
    fail_values: tuple[str, ...] = Field(min_length=1)
    # The cell holding the moment the reading was taken; the row's own stamp when the row has none.
    time_cell: str | None = None
    max_age_seconds: int = Field(ge=1)
    # How far back the rows are read; a streak older than this is only known to reach back this far.
    history_seconds: int = Field(ge=1)


class ModelBetaCriterion(BaseModel):
    """One release criterion: its words, the ticket that owns it, its carriers and the readings its state is computed from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    condition: str = Field(min_length=1)
    proof: str = Field(min_length=1)
    owner_ticket: str = Field(min_length=1)
    carriers: tuple[ModelBetaCarrier, ...] = ()
    probes: tuple[str, ...] = ()
    ledger_readings: tuple[str, ...] = ()


class ModelBetaServeTarget(BaseModel):
    """A host whose factory floor web server also serves the board."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    ssh_target: str = Field(min_length=1)
    remote_dir: str = Field(min_length=1)
    serve_url: str = Field(min_length=1)


class ModelBetaBoardOverlay(BaseModel):
    """The deployment overlay: the milestone, its criteria, carriers, probes, ledger readings and the hosts that serve the page."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    milestone_id: str = Field(min_length=1)
    milestone_title: str = Field(min_length=1)
    # Repositories whose probes are still read from where they were first built; the page names every probe read from one.
    cloud_repositories: tuple[str, ...] = ()
    criteria: tuple[ModelBetaCriterion, ...] = Field(min_length=1)
    probes: tuple[ModelBetaProbe, ...] = ()
    ledger_readings: tuple[ModelBetaLedgerReading, ...] = ()
    # How ledger rows are read: the ledger file named by a variable, or the read command a variable names (the bus route).
    ledger_read: Annotated[
        ModelFileLedgerReadRoute | ModelCommandLedgerReadRoute,
        Field(discriminator="route"),
    ]
    serve_targets: tuple[ModelBetaServeTarget, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _references_resolve(self) -> ModelBetaBoardOverlay:
        probe_ids = [p.id for p in self.probes]
        ledger_ids = [r.id for r in self.ledger_readings]
        ids = [c.id for c in self.criteria]
        for label, found in (
            ("criterion", ids),
            ("probe", probe_ids),
            ("ledger reading", ledger_ids),
        ):
            dupes = sorted({i for i in found if found.count(i) > 1})
            if dupes:
                raise ValueError(f"duplicate {label} ids: {', '.join(dupes)}")
        for criterion in self.criteria:
            for ref in criterion.probes:
                if ref not in probe_ids:
                    raise ValueError(
                        f"criterion {criterion.id} names probe {ref}, which the overlay does not declare"
                    )
            for ref in criterion.ledger_readings:
                if ref not in ledger_ids:
                    raise ValueError(
                        f"criterion {criterion.id} names ledger reading {ref}, which the overlay does not declare"
                    )
        return self


class ModelBetaTransport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    connect_timeout_seconds: int = Field(ge=1)
    transfer_timeout_seconds: int = Field(ge=1)


class ModelBetaBoardContractRuntime(BaseModel):
    """The ``runtime`` block of contract.yaml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    home_env: str = Field(min_length=1)
    onex_relpath: str = Field(min_length=1)
    overlay_id: str = Field(min_length=1)
    overlay_file_env: str = Field(min_length=1)
    ledger_row_cap: int = Field(ge=1)
    output_relpath: str = Field(min_length=1)
    reads_relpath: str = Field(min_length=1)
    page_file: str = Field(min_length=1)
    data_file: str = Field(min_length=1)
    probe_run_limit: int = Field(ge=1)
    probe_read_timeout_seconds: int = Field(ge=1)
    stage_deadline_seconds: int = Field(ge=1)
    probe_refresh_seconds: int = Field(ge=0)
    transport: ModelBetaTransport
    page_refresh_seconds: int = Field(ge=30)


class ModelBetaBoardRuntime(ModelBetaBoardContractRuntime):
    """The contract's runtime with the overlay's declarations."""

    overlay: ModelBetaBoardOverlay


class ModelReadingPoint(BaseModel):
    """One dated verdict of a source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    at: datetime
    verdict: ReadingVerdict
    ref: str = ""


class ModelSourceRead(BaseModel):
    """What one source answered on one read: its dated verdicts newest first, or why it could not be read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    kind: SourceKind
    read: Literal["read", "unread"]
    read_at: datetime
    reason: str = ""
    points: tuple[ModelReadingPoint, ...] = ()

    @field_validator("points")
    @classmethod
    def _newest_first(
        cls, value: tuple[ModelReadingPoint, ...]
    ) -> tuple[ModelReadingPoint, ...]:
        ordered = sorted(value, key=lambda p: p.at, reverse=True)
        return tuple(ordered)


class ModelSourceReads(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reads: tuple[ModelSourceRead, ...] = ()


class ModelSourceState(BaseModel):
    """A source's state on the board."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    kind: SourceKind
    state: SourceState
    observed_at: datetime | None = None
    age_seconds: int | None = None
    since: datetime | None = None
    # True when the streak of equal verdicts reaches the oldest reading the source gave, so the true start is earlier.
    since_is_lower_bound: bool = False
    detail: str = ""
    ref: str = ""
    repository: str | None = None
    workflow: str | None = None


class ModelCriterionState(BaseModel):
    """One criterion's state, since when, its owner ticket, its carriers and the sources behind the state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    condition: str
    proof: str
    owner_ticket: str
    carriers: tuple[str, ...]
    state: CriterionState
    since: datetime | None = None
    since_is_lower_bound: bool = False
    reading_age_seconds: int | None = None
    reason: str
    sources: tuple[ModelSourceState, ...]


class ModelCloudProbe(BaseModel):
    """A probe still read from a cloud repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_id: str
    repository: str
    workflow: str
    criteria: tuple[str, ...]
    state: SourceState


class ModelBetaBoard(BaseModel):
    """The board the page is drawn from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: datetime
    milestone_id: str
    milestone_title: str
    met: bool
    counts: dict[str, int]
    criteria: tuple[ModelCriterionState, ...]
    cloud_probes: tuple[ModelCloudProbe, ...]
    cloud_repositories: tuple[str, ...]


class ModelBetaBoardRequest(BaseModel):
    """The stage to run and the registry home."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: Literal["build", "publish", "run"]
    home: str | None = None


class ModelTargetPublish(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    serve_url: str
    ok: bool
    detail: str = ""


class ModelBetaBoardResult(BaseModel):
    """Whether the stage ran, what it wrote and where the page was published."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    stage: Literal["build", "publish", "run"]
    detail: str = ""
    wrote: str = ""
    met: bool | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    targets: tuple[ModelTargetPublish, ...] = ()
