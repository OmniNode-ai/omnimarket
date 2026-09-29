# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19790: label replacement, rule-7a dispatch, and tenant-safe persistence."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_projection_delegation_eval.handlers import (
    DelegationEvalProjectionWriter,
    HandlerProjectionDelegationEval,
)
from omnimarket.nodes.node_projection_delegation_eval.models import (
    ModelDelegationEvalProjectionRequest,
)
from omnimarket.projection.runner import BaseProjectionRunner

pytestmark = pytest.mark.unit
_IN_TOPIC = "onex.evt.omnimarket.delegation-eval-item-labelled.v1"  # onex-topic-allow: asserted against the contract
_OUT_TOPIC = "onex.evt.omnimarket.projection-delegation-eval-applied.v1"  # onex-topic-allow: asserted against the contract
_DLQ_TOPIC = "onex.dlq.omnimarket.projection-delegation-eval-malformed.v1"  # onex-topic-allow: asserted against the contract
_NODE = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_delegation_eval"
)
_T0 = datetime(2026, 9, 28, tzinfo=UTC)
_TENANT = "11111111-1111-1111-1111-111111111111"


def _event(**updates: Any) -> dict[str, Any]:
    return {
        "tenant_id": _TENANT,
        "correlation_id": "call-1",
        "attempt_index": 0,
        "task_class": "code",
        "stratum": "hard",
        "prompt_snapshot": "explain this",
        "response_snapshot": "an explanation",
        "gate_verdict": "pass",
        "deciding_check": "tests",
        "label": "correct",
        "rater_role": "human",
        "rubric_version": "v1",
        "computed_facts": {"passed": True},
        "observed_at": _T0,
    } | updates


class _Store:
    """Loop-bound adapter double; validates SQL grain and typed DB arguments."""

    def __init__(self) -> None:
        self.rows: dict[tuple[Any, ...], dict[str, Any]] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queries: list[str] = []

    async def connect(self) -> None:
        assert self.loop is None
        self.loop = asyncio.get_running_loop()

    async def close(self) -> None:
        self.loop = None

    async def execute(
        self, query: str, *params: Any, tenant: str | None = None
    ) -> list[dict[str, Any]]:
        assert self.loop is asyncio.get_running_loop()
        assert "ON CONFLICT (tenant_id, item_key, rater_role, rubric_version)" in query
        assert "observed_at < EXCLUDED.observed_at" in query
        assert "label = EXCLUDED.label" in query
        assert "RETURNING" in query
        self.queries.append(query)
        columns = query.split("(", 1)[1].split(")", 1)[0].replace("\n", "").split(",")
        row = dict(
            zip([c.strip() for c in columns][: len(params)], params, strict=True)
        )
        assert isinstance(row["tenant_id"], UUID)
        assert tenant == str(row["tenant_id"])
        assert isinstance(row["observed_at"], datetime)
        row["computed_facts"] = json.loads(row["computed_facts"])
        key = tuple(
            row[c] for c in ("tenant_id", "item_key", "rater_role", "rubric_version")
        )
        if key in self.rows and self.rows[key]["observed_at"] >= row["observed_at"]:
            return []
        self.rows[key] = row
        return [{"projection_cursor": len(self.rows)}]


def _writer() -> tuple[DelegationEvalProjectionWriter, _Store]:
    writer = DelegationEvalProjectionWriter()
    store = _Store()
    writer._db = store  # type: ignore[assignment]
    return writer, store


def test_one_row_per_label_key() -> None:
    writer, store = _writer()
    assert writer.handle(_event())["rows_written"] == 1
    key = (UUID(_TENANT), "call-1:0", "human", "v1")
    assert list(store.rows) == [key]
    assert (
        writer.handle(
            _event(label="incorrect", observed_at=_T0 + timedelta(seconds=1))
        )["rows_written"]
        == 1
    )
    assert len(store.rows) == 1
    assert store.rows[key]["label"] == "incorrect"
    assert writer.handle(_event(rater_role="judge"))["rows_written"] == 1
    assert len(store.rows) == 2


@pytest.mark.parametrize(
    "updates",
    [{"rubric_version": "v2"}, {"tenant_id": "22222222-2222-2222-2222-222222222222"}],
)
def test_distinct_rubrics_and_tenants_are_separate(updates: dict[str, Any]) -> None:
    writer, store = _writer()
    writer.handle(_event())
    writer.handle(_event(**updates))
    assert len(store.rows) == 2


def test_stale_and_duplicate_labels_write_zero_rows() -> None:
    writer, store = _writer()
    writer.handle(_event())
    for observed_at in (_T0, _T0 - timedelta(seconds=1)):
        assert (
            writer.handle(_event(label="stale", observed_at=observed_at))[
                "rows_written"
            ]
            == 0
        )
    assert next(iter(store.rows.values()))["label"] == "correct"
    assert store.loop is None


def test_the_contract_declares_the_topics_the_writer_subscribes() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    assert contract["event_bus"] == {
        "subscribe_topics": [_IN_TOPIC],
        "publish_topics": [_OUT_TOPIC],
        "dlq_topics": [_DLQ_TOPIC],
    }
    assert contract["terminal_event"] == _OUT_TOPIC
    assert _writer()[0].subscribe_topics == [_IN_TOPIC]
    table = contract["db_io"]["db_tables"][0]
    assert (table["schema"], table["name"], table["access"]) == (
        "public",
        "delegation_eval_items",
        "read_write",
    )
    assert {h["handler"]["name"] for h in contract["handler_routing"]["handlers"]} == {
        "HandlerProjectionDelegationEval",
        "DelegationEvalProjectionWriter",
    }


def test_the_writer_declares_in_process_dispatch() -> None:
    assert issubclass(DelegationEvalProjectionWriter, BaseProjectionRunner)
    assert DelegationEvalProjectionWriter.onex_runtime_inprocess_dispatch is True


def test_the_writer_entry_returns_a_row_count() -> None:
    writer, _ = _writer()
    assert isinstance(writer._derive, HandlerProjectionDelegationEval)
    result = writer.handle(_event())
    assert type(result["rows_written"]) is int
    # Prompt/response content belongs only in the lab table, never the applied event.
    assert "prompt_snapshot" not in json.dumps(result)
    assert "response_snapshot" not in json.dumps(result)


def test_fold_determinism_and_envelope_timestamp() -> None:
    event = _event()
    event["_envelope_timestamp"] = event.pop("observed_at")
    request = ModelDelegationEvalProjectionRequest.model_validate(event)
    fold = HandlerProjectionDelegationEval()
    assert fold.handle(request) == fold.handle(request)
    row = fold.handle(request).rows[0]
    assert row.item_key == "call-1:0"
    assert row.observed_at == _T0
    assert row.computed_facts == {"passed": True}


def test_missing_event_time_and_tenant_fail_closed() -> None:
    for field in ("observed_at", "tenant_id"):
        event = _event()
        del event[field]
        with pytest.raises(ValidationError):
            ModelDelegationEvalProjectionRequest.model_validate(event)


def test_migrations_enforce_tenant_isolation_and_runtime_grants() -> None:
    ddl = (_NODE / "migrations/0000_create_delegation_eval_items.sql").read_text()
    assert "tenant_id UUID NOT NULL" in ddl
    assert "PRIMARY KEY (tenant_id, item_key, rater_role, rubric_version)" in ddl
    assert "ENABLE ROW LEVEL SECURITY" in ddl
    assert "FORCE ROW LEVEL SECURITY" in ddl
    assert "CREATE POLICY tenant_isolation" in ddl
    assert "USING (tenant_id = current_setting('app.tenant_id', true)::uuid)" in ddl
    assert (
        "WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)" in ddl
    )
    grants = (
        _NODE / "migrations/0001_grant_omninode_runtime_delegation_eval_items.sql"
    ).read_text()
    assert "GRANT SELECT, INSERT, UPDATE" in grants
    assert "ON SEQUENCE public.delegation_eval_items_projection_cursor_seq" in grants
    assert "TO omninode_runtime" in grants
