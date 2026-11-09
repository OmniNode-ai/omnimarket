# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Map one agent observability delivery to the row it writes.

Pure: no clock beyond the ingestion default the retired consumer also used, no
database. ``handle`` validates the payload against the topic's model and returns
the table, conflict column and column values.

Two events (agent actions and agent status) arrive with no ``id``; the retired
consumer minted a random one, so a replayed event wrote a second row. Here the id
is derived from the topic and the event's content, so the same event names the same
row on every replay, and a redelivery changes nothing. The delivery's partition and offset are part of
that content, so two distinct events that carry the same payload are two rows.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import yaml
from pydantic import BaseModel

from omnimarket.nodes.node_projection_agent_actions.handlers.agent_actions_tables import (
    TABLE_SPECS,
    TableSpec,
)
from omnimarket.nodes.node_projection_agent_actions.models import (
    ModelAgentActionsDelivery,
    ModelAgentActionsRow,
)
from omnimarket.topic_namespace import strip_topic_namespace

_CONTRACT = Path(__file__).parent.parent / "contract.yaml"


class UnknownAgentActionsTopicError(ValueError):
    """A delivery from a topic this node does not project."""


def topic_tables(contract_path: Path = _CONTRACT) -> dict[str, str]:
    """Topic to table, read from the contract's ``topic_tables`` declaration."""
    with open(contract_path) as handle:
        declared: dict[str, str] = yaml.safe_load(handle)["topic_tables"]
    return declared


def derived_id(
    topic: str, payload: dict[str, object], partition: int = 0, offset: int = 0
) -> str:
    """The row id for an event that carries none: a function of topic, position and content."""
    content = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return str(uuid5(NAMESPACE_URL, f"{topic}#{partition}#{offset}#{digest}"))


def _column_value(spec: TableSpec, model: BaseModel, column: str) -> object:
    value: Any = getattr(model, column)
    if column in spec.json_columns and value is not None:
        return json.dumps(list(value) if isinstance(value, tuple) else dict(value))
    return value


class HandlerProjectionAgentActions:
    """Validate one delivery and return its row."""

    def __init__(self, contract_path: Path = _CONTRACT) -> None:
        self._tables = topic_tables(contract_path)

    def handle(self, request: ModelAgentActionsDelivery) -> ModelAgentActionsRow:
        topic = strip_topic_namespace(request.topic)
        table = self._tables.get(topic)
        if table is None:
            raise UnknownAgentActionsTopicError(request.topic)
        spec = TABLE_SPECS[table]
        payload = {k: v for k, v in request.payload.items() if not k.startswith("_")}
        if spec.derives_id and not payload.get("id"):
            payload["id"] = derived_id(
                topic, payload, request.partition, request.offset
            )
        if spec.derives_id and "created_at" not in payload:
            # The producer sends its event time as ``timestamp``; the retired
            # consumer ignored it and stamped ingestion time. Event time keeps a
            # replay deterministic; ingestion time is the fallback when absent.
            event_time = payload.get("timestamp") or datetime.now(UTC)
            if "timestamp" not in spec.model.model_fields:
                payload.pop("timestamp", None)  # a model that forbids extras refuses it
            payload["created_at"] = event_time
        model = spec.model.model_validate(payload)
        return ModelAgentActionsRow(
            table=spec.table,
            conflict_column=spec.conflict_column,
            values={
                column: _column_value(spec, model, column)
                for column in spec.bound_columns
            },
        )


__all__ = [
    "HandlerProjectionAgentActions",
    "UnknownAgentActionsTopicError",
    "derived_id",
    "topic_tables",
]
