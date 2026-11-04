# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Commands and receipts for append-only item transitions."""

from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.models.model_ask_state import ModelAskStateReport, ModelAskStateRequest
from omnimarket.models.work_ledger_append import ModelWorkLedgerAppendReceipt


class ModelAskStateTransition(BaseModel):
    """One state intent, fenced by full content digest and the CLAIM's run id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str = Field(min_length=1)
    state: Literal["owned", "parked", "done"]
    evidence: str = ""
    revision: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    at: Annotated[datetime, AwareDatetime]
    reason: str | None = None
    review_by: Annotated[datetime, AwareDatetime] | None = None
    seq: int | None = Field(default=None, ge=1)


class ModelAskStateTransitionRequest(BaseModel):
    """Current sources and explicit intents, or CLAIM/TERMINAL consumption when intents are empty."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot: ModelAskStateRequest
    transitions: tuple[ModelAskStateTransition, ...] = ()
    requested_by_lane: str = Field(min_length=1)
    requesting_host: str = Field(min_length=1)


class ModelAskStateTransitionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[str, ...] = ()
    receipts: tuple[ModelWorkLedgerAppendReceipt, ...] = ()
    skipped: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    state: ModelAskStateReport


class ModelAskStateTransitionRuntime(BaseModel):
    """Contract-owned wiring and timeout. Transport and credentials come from its overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_root_env: str = Field(min_length=1)
    append_timeout_seconds: float = Field(gt=0)
    ledger_id: str = Field(min_length=1)


class ModelAskCompletionSources(BaseModel):
    """Read-only evidence sources bound by the transition node's deployment overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    watcher_state_path: Path | None = None
    receipt_root: Path | None = None
    ruling_ledger_path: Path | None = None
    max_age_seconds: int = Field(gt=0)


class ModelAskPrWatcherFacts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    state: str
    merged_at: str = ""


class ModelAskPrWatcherRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    facts: ModelAskPrWatcherFacts


class ModelAskPrWatcherSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    last_tick: Annotated[datetime, AwareDatetime]
    prs: dict[str, ModelAskPrWatcherRecord]


class ModelAskCompletionReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    revision: str
    run_id: str
    outcome: str
