# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19987: the Workflow page's exposure serves one run's steps in step order.

Step events are folded and stored in a local SQLite store the way the local
writer does, then read back through the projection read node exactly as
``onex dashboard`` serves them, with the exposures built from the node contracts
(no hand-built topic map), so the contract declaration is what is under test.

Failure modes each test is written against:

* the served order follows delivery or the clock, so the page would have to sort
  in the browser (AC1);
* the exposure is not tenant-scoped: another tenant's step with the same
  correlation id is served inside this tenant's run (AC2);
* the exposure cannot be read for one run, so the page gets every run's steps;
* a second run's steps are interleaved with the requested run's;
* the served row leaves out a field the timeline renders (kind, time, duration,
  outcome, cause, backend, candidates, the failing-step pointer).
"""

from __future__ import annotations

import json
import random
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from omnimarket.models.model_delegation_run_step import ModelDelegationRunStep
from omnimarket.nodes.node_local_dashboard_serve_effect.handlers.handler_local_dashboard_serve import (
    create_dashboard_app,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.nodes.node_projection_run_trace.handlers.handler_projection_run_trace import (
    HandlerProjectionRunTrace,
)
from omnimarket.nodes.node_projection_run_trace.handlers.handler_run_trace_store import (
    apply_run_step,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

RUN_TRACE = "onex.snapshot.projection.run-trace.v1"
OTHER_TENANT = "11111111-2222-4333-8444-555555555555"
OTHER_RUN = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
CHAIN_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "omn19987"
    / "run_step_chain.json"
)


def _chain() -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = json.loads(CHAIN_PATH.read_text(encoding="utf-8"))
    return chain


def _recorded() -> list[dict[str, Any]]:
    """The step events one real delegation emitted on a lab host, as published."""
    chain: list[dict[str, Any]] = json.loads(
        CHAIN_PATH.with_name("recorded_run_step_chain.json").read_text(encoding="utf-8")
    )
    return chain


_CHAINS = {"recorded": _recorded, "constructed": _chain}


def _store(tmp_path: Path, events: list[dict[str, Any]]) -> Path:
    db_path = tmp_path / "delegation.sqlite"
    adapter = SqliteDatabaseAdapter(db_path)
    fold = HandlerProjectionRunTrace()
    for event in events:
        apply_run_step(
            fold.handle(ModelDelegationRunStep.model_validate(event)), adapter
        )
    return db_path


@cache
def _topics() -> dict[str, ProjectionTableConfig]:
    """The exposures exactly as the node contracts declare them."""
    return build_projection_topic_map()


def _read(
    db_path: Path, tenant: str, correlation_id: str | None
) -> list[dict[str, Any]]:
    topics = _topics()
    handler = HandlerProjectionRead(
        topic_map=topics, row_source=SqliteTableRowSource(db_path)
    )
    client = TestClient(
        create_dashboard_app(handler=handler, topic_map=topics, tenant=tenant)
    )
    params = {} if correlation_id is None else {"correlation_id": correlation_id}
    response = client.get(f"/projection/{RUN_TRACE}", params=params)
    assert response.status_code == 200, response.json()
    rows: list[dict[str, Any]] = response.json()["rows"]
    return rows


@pytest.mark.parametrize("which", sorted(_CHAINS))
@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
def test_a_shuffled_chain_with_equal_timestamps_and_a_foreign_step_is_served_in_step_order(
    tmp_path: Path, seed: int, which: str
) -> None:
    chain = _CHAINS[which]()
    tenant = chain[0]["tenant_id"]
    run = chain[0]["correlation_id"]
    chain[2]["started_at"] = chain[1]["started_at"]
    chain[-1]["started_at"] = chain[0]["started_at"]
    foreign = {**chain[3], "tenant_id": OTHER_TENANT, "reason": "foreign"}
    delivered = [*chain, foreign]
    random.Random(seed).shuffle(delivered)
    assert [e for e in delivered if e is not foreign] != chain

    served = _read(_store(tmp_path, delivered), tenant, run)

    assert [row["step_sequence"] for row in served] == [
        event["step_sequence"] for event in chain
    ]
    assert [row["step_kind"] for row in served] == [e["step_kind"] for e in chain]
    assert {row["tenant_id"] for row in served} == {tenant}
    assert "foreign" not in {row["reason"] for row in served}


def test_the_other_tenant_reads_only_its_own_step(tmp_path: Path) -> None:
    chain = _chain()
    foreign = {**chain[3], "tenant_id": OTHER_TENANT, "reason": "foreign"}
    db_path = _store(tmp_path, [*chain, foreign])
    theirs = _read(db_path, OTHER_TENANT, chain[0]["correlation_id"])
    assert [(row["tenant_id"], row["reason"]) for row in theirs] == [
        (OTHER_TENANT, "foreign")
    ]


def test_one_run_is_served_without_another_runs_steps(tmp_path: Path) -> None:
    chain = _chain()
    tenant = chain[0]["tenant_id"]
    # A second run of the same tenant, over the same seconds, delivered between
    # the first run's steps.
    other_run = [{**event, "correlation_id": OTHER_RUN} for event in chain[:3]]
    delivered = [
        chain[0],
        other_run[2],
        *chain[1:4],
        other_run[0],
        *chain[4:],
        other_run[1],
    ]
    db_path = _store(tmp_path, delivered)

    served = _read(db_path, tenant, chain[0]["correlation_id"])
    assert {row["correlation_id"] for row in served} == {chain[0]["correlation_id"]}
    assert [row["step_sequence"] for row in served] == [
        event["step_sequence"] for event in chain
    ]
    served_other = _read(db_path, tenant, OTHER_RUN)
    assert {row["correlation_id"] for row in served_other} == {OTHER_RUN}
    assert [row["step_sequence"] for row in served_other] == [1, 2, 3]


def test_a_run_with_no_steps_is_served_as_no_rows_not_an_error(tmp_path: Path) -> None:
    chain = _chain()
    db_path = _store(tmp_path, chain)
    assert _read(db_path, chain[0]["tenant_id"], OTHER_RUN) == []


def test_the_served_row_carries_everything_the_timeline_renders(tmp_path: Path) -> None:
    chain = _chain()
    served = _read(
        _store(tmp_path, chain), chain[0]["tenant_id"], chain[0]["correlation_id"]
    )
    for row in served:
        assert {
            "tenant_id",
            "correlation_id",
            "step_sequence",
            "step_kind",
            "started_at",
            "duration_ms",
            "outcome",
            "failure_cause",
            "backend_id",
            "model_id",
            "attempt_number",
            "reason",
            "candidates",
            "failing_step_sequence",
        } <= set(row)
    by_sequence = {row["step_sequence"]: row for row in served}
    failed_gate = by_sequence[4]
    assert failed_gate["outcome"] == "failed"
    assert failed_gate["failure_cause"] == "quality_gate_failed"
    assert by_sequence[3]["duration_ms"] == 2140
    assert by_sequence[1]["duration_ms"] is None
    assert by_sequence[2]["candidates"] == ["local", "cheap_cloud", "frontier"]
