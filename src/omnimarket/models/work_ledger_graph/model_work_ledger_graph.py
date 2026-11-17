# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work ledger graph: rows, entities and typed edges taken from the rows'
own key=value cells. Shared by node_work_ledger_graph_compute (which derives
it) and node_work_ledger_graph_query_effect (which answers the five queries)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

WORK_LEDGER_GRAPH_SCHEMA_VERSION = "work-ledger-graph/v1"


class ModelLedgerGraphRow(BaseModel):
    """One ledger row as the projection holds it (a subset of
    ModelWorkLedgerRowRecord): the id, timestamp, type and the raw row text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str = Field(min_length=1)
    row_ts: str = Field(min_length=1)
    row_type: str = Field(min_length=1)
    raw_row: str


class EnumLedgerEdgeKind(StrEnum):
    IN_LANE = "in_lane"
    NAMES_TICKET = "names_ticket"
    PARENT_TICKET = "parent_ticket"
    MECHANISM_TICKET = "mechanism_ticket"
    PARENT_LANE = "parent_lane"
    NAMES_PR = "names_pr"
    NAMES_HEAD = "names_head"
    INSTALLED = "installed"
    MSG_FROM = "msg_from"
    MSG_TO = "msg_to"
    HANDED_TO = "handed_to"
    OPENS_CLAIM = "opens_claim"
    CLOSES_CLAIM = "closes_claim"
    NAMES_CHECK = "names_check"
    NAMES_CAUSE = "names_cause"


class ModelLedgerGraphCell(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    value: str


class ModelLedgerGraphRowNode(BaseModel):
    """A row node: its id, timestamp, type, lane and every key=value cell."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str
    row_ts: str
    row_type: str
    lane: str | None = None
    cells: tuple[ModelLedgerGraphCell, ...] = ()


class ModelLedgerGraphEdge(BaseModel):
    """A typed edge from a row node to an entity node (``kind:value``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str
    kind: EnumLedgerEdgeKind
    target: str


class ModelWorkLedgerGraph(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = WORK_LEDGER_GRAPH_SCHEMA_VERSION
    row_count: int = Field(ge=0)
    entity_count: int = Field(ge=0)
    rows: tuple[ModelLedgerGraphRowNode, ...] = ()
    edges: tuple[ModelLedgerGraphEdge, ...] = ()
    content_sha256: str = ""


class ModelWorkLedgerGraphComputeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[ModelLedgerGraphRow, ...]


class EnumWorkLedgerGraphQuery(StrEnum):
    OPEN_PRS_NO_LIVE_OWNER = "open_prs_no_live_owner"
    HEADS_REVIEWED_MORE_THAN_ONCE = "heads_reviewed_more_than_once"
    RED_CHECK_HOLDING_MOST_OPEN_PRS = "red_check_holding_most_open_prs"
    PROCESS_RULINGS_NO_MECHANISM = "process_rulings_no_mechanism"
    SEEN_RUNNING_TICKET_OPEN = "seen_running_ticket_open"


class EnumWorkLedgerGraphQueryStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


class ModelWorkLedgerGraphQueryRequest(BaseModel):
    """One typed query over a graph, given inline or as a JSON file path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: EnumWorkLedgerGraphQuery
    graph: ModelWorkLedgerGraph | None = None
    graph_path: str | None = None


class ModelWorkLedgerGraphAnswer(BaseModel):
    """One answer: its subject (a PR, head, check, ruling row or ticket), the
    supporting facts as cells and the row ids that carry them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str
    facts: tuple[ModelLedgerGraphCell, ...] = ()
    row_ids: tuple[str, ...] = ()


class ModelWorkLedgerGraphQueryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    query: EnumWorkLedgerGraphQuery
    status: EnumWorkLedgerGraphQueryStatus
    error: str | None = None
    graph_sha256: str | None = None
    answers: tuple[ModelWorkLedgerGraphAnswer, ...] = ()
    elapsed_ms: float = Field(default=0.0, ge=0.0)
    model_tokens: int = Field(default=0, ge=0)


__all__ = [
    "WORK_LEDGER_GRAPH_SCHEMA_VERSION",
    "EnumLedgerEdgeKind",
    "EnumWorkLedgerGraphQuery",
    "EnumWorkLedgerGraphQueryStatus",
    "ModelLedgerGraphCell",
    "ModelLedgerGraphEdge",
    "ModelLedgerGraphRow",
    "ModelLedgerGraphRowNode",
    "ModelWorkLedgerGraph",
    "ModelWorkLedgerGraphAnswer",
    "ModelWorkLedgerGraphComputeRequest",
    "ModelWorkLedgerGraphQueryRequest",
    "ModelWorkLedgerGraphQueryResult",
]
