# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the work ledger graph.

The graph is derived from ``omninode_internal.work_ledger_rows`` by the rows' own
``key=value`` cells, never by a model reading the free text. Rows, nodes and edges
are plain typed records so two derivations over the same rows serialise to the
same bytes.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ModelWorkLedgerGraphRow(BaseModel):
    """One projected ledger row: the columns of work_ledger_rows the graph reads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str = Field(min_length=1)
    row_ts: datetime
    row_type: str = Field(min_length=1)
    row_lane: str | None = None
    tickets: tuple[str, ...] = ()
    raw_row: str = Field(min_length=1)


class ModelWorkLedgerGraphNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    attrs: tuple[tuple[str, str], ...] = ()


class ModelWorkLedgerGraphEdge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    src: str = Field(min_length=1)
    rel: str = Field(min_length=1)
    dst: str = Field(min_length=1)
    row_id: str = Field(min_length=1)


class ModelWorkLedgerGraphRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(default_factory=uuid4)
    rows: tuple[ModelWorkLedgerGraphRow, ...]


class ModelWorkLedgerGraph(BaseModel):
    """The derived graph. ``digest`` is the sha256 of the canonical node and edge JSON."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_count: int = Field(ge=0)
    nodes: tuple[ModelWorkLedgerGraphNode, ...]
    edges: tuple[ModelWorkLedgerGraphEdge, ...]
    digest: str = Field(min_length=64, max_length=64)


class EnumWorkLedgerGraphQuery(StrEnum):
    """The five typed questions the query effect answers."""

    OPEN_PRS_WITHOUT_OWNER = "open_prs_without_owner"
    HEADS_REVIEWED_MORE_THAN_ONCE = "heads_reviewed_more_than_once"
    RED_CHECK_HOLDING_MOST_PRS = "red_check_holding_most_prs"
    PROCESS_RULINGS_WITHOUT_MECHANISM = "process_rulings_without_mechanism"
    SEEN_RUNNING_WITH_OPEN_TICKETS = "seen_running_with_open_tickets"


class ModelWorkLedgerGraphQueryRequest(BaseModel):
    """One typed question over a window of the work ledger.

    ``open_prs`` and ``closed_tickets`` are optional outside state (the PR watcher's open
    set, the tracker's closed tickets). The ledger alone does not record whether a PR is
    open or a ticket is closed; when they are absent the answer says which ledger rule it
    used instead.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(default_factory=uuid4)
    query: EnumWorkLedgerGraphQuery
    since: datetime | None = None
    until: datetime | None = None
    open_prs: tuple[str, ...] | None = None
    closed_tickets: tuple[str, ...] | None = None
    limit: int = Field(default=200, ge=1, le=5000)


class ModelWorkLedgerGraphAnswerItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str = Field(min_length=1)
    count: int = Field(default=0, ge=0)
    prs: tuple[str, ...] = ()
    tickets: tuple[str, ...] = ()
    lanes: tuple[str, ...] = ()
    row_ids: tuple[str, ...] = ()
    detail: str = ""


class ModelWorkLedgerGraphQueryResult(BaseModel):
    """The answer. ``status`` is ``error`` exactly when ``error`` is set; an error is never empty."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    query: EnumWorkLedgerGraphQuery
    status: Literal["ok", "error"]
    error: str | None = None
    window_since: datetime | None = None
    window_until: datetime | None = None
    rows_read: int = Field(default=0, ge=0)
    graph_digest: str | None = None
    open_rule: str = ""
    matched: int = Field(default=0, ge=0)
    items: tuple[ModelWorkLedgerGraphAnswerItem, ...] = ()
    elapsed_seconds: float = Field(default=0.0, ge=0.0)


__all__ = [
    "EnumWorkLedgerGraphQuery",
    "ModelWorkLedgerGraph",
    "ModelWorkLedgerGraphAnswerItem",
    "ModelWorkLedgerGraphEdge",
    "ModelWorkLedgerGraphNode",
    "ModelWorkLedgerGraphQueryRequest",
    "ModelWorkLedgerGraphQueryResult",
    "ModelWorkLedgerGraphRequest",
    "ModelWorkLedgerGraphRow",
]
