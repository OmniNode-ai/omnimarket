# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and report of node_ask_state_compute (OMN-21713).

The report is the schema ``omni.ask-state/1``. Another view (the factory floor record) embeds
``ModelAskStateReport`` or only its ``parked_queue``; every field is plain data so a consumer
can carry it unchanged.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EnumAskState(StrEnum):
    """Where one operator ask stands.

    ``new`` is an ask younger than the grace window with no row yet: it becomes ``dropped``
    when the window passes with still no owner, ticket or parked row.
    """

    OWNED = "owned"
    TICKETED = "ticketed"
    PARKED = "parked"
    DONE = "done"
    DROPPED = "dropped"
    NEW = "new"


class EnumAskLink(StrEnum):
    """How the ledger rows were tied to the ask: by id, by words, or not at all."""

    EXPLICIT = "explicit"
    INFERRED = "inferred"
    NONE = "none"


class ModelAskPromptItem(BaseModel):
    """One classified item of a captured operator prompt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    subject: str
    confidence: float = Field(ge=0.0, le=1.0)


class ModelAskPrompt(BaseModel):
    """One operator prompt from the capture store, with the items the capture classified."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capture_id: str = Field(min_length=1)
    session_id: str
    said_at: datetime
    text: str
    items: tuple[ModelAskPromptItem, ...] = ()


class ModelAskPromoteRule(BaseModel):
    """Wording that makes an item of an untracked kind an ask: a challenge or a directive is a request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kinds: tuple[str, ...] = Field(min_length=1)
    pattern: str = Field(min_length=1)


class ModelAskStateConfig(BaseModel):
    """Vocabularies and limits. The handler holds none; the contract's config block supplies them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_id: str = Field(min_length=1)
    tracked_kinds: tuple[str, ...] = Field(min_length=1)
    grace_minutes: int = Field(ge=0)
    link_window_hours: int = Field(ge=1)
    min_shared_terms: int = Field(ge=1)
    min_shared_fraction: float = Field(gt=0.0, le=1.0)
    stem_chars: int = Field(ge=3)
    min_term_chars: int = Field(ge=1)
    ask_term_limit: int = Field(ge=1)
    max_claim_frequency: float = Field(gt=0.0, le=1.0)
    common_min_claims: int = Field(ge=1)
    restate_threshold: int = Field(ge=2)
    restate_kinds: tuple[str, ...] = Field(min_length=1)
    restate_min_shared: int = Field(ge=2)
    restate_common_frequency: float = Field(gt=0.0, le=1.0)
    restate_common_min_statements: int = Field(ge=1)
    umbrella_lanes: int = Field(ge=1)
    restate_lookback_days: int = Field(ge=0)
    after_cell: str = Field(min_length=1)
    pr_cells: tuple[str, ...] = Field(min_length=1)
    reference_cells: tuple[str, ...] = Field(min_length=1)
    blocked_outcomes: tuple[str, ...]
    friction_kind: str = Field(min_length=1)
    leverage_reason_edges: int = Field(ge=1)
    min_quote_chars: int = Field(ge=1)
    done_outcomes: tuple[str, ...]
    handoff_outcomes: tuple[str, ...]
    capture_lane: str = Field(min_length=1)
    ask_cells: tuple[str, ...] = Field(min_length=1)
    closes_ask_cell: str = Field(min_length=1)
    state_cell: str = Field(min_length=1)
    parked_state: str = Field(min_length=1)
    ticketed_state: str = Field(min_length=1)
    reason_cell: str = Field(min_length=1)
    seq_cell: str = Field(min_length=1)
    excerpt_chars: int = Field(ge=20)
    owned_state: str = Field(min_length=1)
    owner_cell: str = Field(min_length=1)
    done_state: str = Field(min_length=1)
    answer_kinds: tuple[str, ...] = Field(
        description="row kinds that deliver an answer; one citing closes-ask= closes the ask by itself",
    )
    send_kinds: tuple[str, ...] = Field(
        description="answer kinds that close an ask by citing it with ask= alone: a send is a delivery",
    )
    promote_rules: tuple[ModelAskPromoteRule, ...] = Field(
        description="wording that promotes an untracked item (a why-challenge, a directive) to an ask",
    )
    promote_unclassified: bool = Field(
        description="read the sentences of a prompt the capture classified into no item",
    )
    wip_limit: int | None = Field(
        default=None,
        ge=1,
        description="lanes allowed in flight at once; None is UNKNOWN, never read as unlimited",
    )


class ModelAskStateRequest(BaseModel):
    """Captured prompts, ledger rows, the window start and the time the report is judged at."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompts: tuple[ModelAskPrompt, ...]
    rows: tuple[str, ...]
    now: datetime
    since: datetime
    config: ModelAskStateConfig


class ModelAskEntry(BaseModel):
    """One tracked ask and its state, with the rows that decided it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ask_id: str
    item_id: str
    kind: str
    said_at: datetime
    session_id: str
    words: str
    subject: str
    confidence: float
    state: EnumAskState
    owner_lane: str | None = None
    tickets: tuple[str, ...] = ()
    park_reason: str | None = None
    park_seq: int | None = None
    link: EnumAskLink = EnumAskLink.NONE
    resolution: str | None = Field(
        default=None,
        description="how a done ask closed: terminal, closes-ask, ruling or withdrawn",
    )
    evidence: tuple[str, ...] = Field(
        default=(), description="the deciding rows as '<TYPE> <stamp> lane=<lane>'"
    )
    shared_terms: tuple[str, ...] = Field(
        default=(), description="the words an inferred link rests on"
    )
    age_minutes: float
    restated: int = Field(
        default=1,
        ge=1,
        description="distinct statements of this direction in captured prompts and rulings, itself included",
    )
    restated_flag: bool = Field(
        default=False,
        description="restated at least the configured threshold: needs its mechanism, not another reminder",
    )
    leverage: int = Field(
        default=0, ge=0, description="dependency edges found that this ask unblocks"
    )
    leverage_reason: str = ""


class ModelParkedAsk(BaseModel):
    """One parked ask, in queue order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    position: int = Field(ge=1)
    ask_id: str
    words: str
    reason: str | None
    seq: int | None
    said_at: datetime
    tickets: tuple[str, ...] = ()
    restated: int = Field(default=1, ge=1)
    restated_flag: bool = False
    leverage: int = Field(default=0, ge=0)
    leverage_reason: str = ""
    within_wip: bool | None = Field(
        default=None,
        description="inside the free WIP slots at this position; None while the limit is UNKNOWN",
    )


class ModelAskCounts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    owned: int = 0
    ticketed: int = 0
    parked: int = 0
    done: int = 0
    dropped: int = 0
    new: int = 0
    total: int = 0


class ModelAskWip(BaseModel):
    """Lanes in flight on asks against the limit; ``unknown`` while no limit is configured."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    owned_lanes: tuple[str, ...] = ()
    limit: int | None = None
    status: Literal["within", "over", "unknown"] = "unknown"


class ModelKindCount(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    count: int


class ModelUnclassifiedPrompt(BaseModel):
    """A prompt the capture classified into no item: visible, because a classifier miss is a silent drop."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capture_id: str
    said_at: datetime
    text: str


class ModelAskStateReport(BaseModel):
    """Every tracked ask with its state, the parked queue, the dropped ids and the counts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_id: str
    generated_at: datetime
    since: datetime
    asks: tuple[ModelAskEntry, ...] = ()
    parked_queue: tuple[ModelParkedAsk, ...] = ()
    dropped: tuple[str, ...] = ()
    restated_asks: tuple[str, ...] = ()
    counts: ModelAskCounts = ModelAskCounts()
    wip: ModelAskWip = ModelAskWip()
    not_tracked: tuple[ModelKindCount, ...] = ()
    unclassified: tuple[ModelUnclassifiedPrompt, ...] = ()
    text: str = ""


__all__ = [
    "EnumAskLink",
    "EnumAskState",
    "ModelAskCounts",
    "ModelAskEntry",
    "ModelAskPrompt",
    "ModelAskPromptItem",
    "ModelAskStateConfig",
    "ModelAskStateReport",
    "ModelAskStateRequest",
    "ModelAskWip",
    "ModelKindCount",
    "ModelParkedAsk",
    "ModelUnclassifiedPrompt",
]
