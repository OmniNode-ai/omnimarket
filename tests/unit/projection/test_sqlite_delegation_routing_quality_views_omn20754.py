# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A local store serves the model-routing and quality-gate exposures (OMN-20754).

On Postgres ``projection_delegation_model_routing`` (migration 0055) and
``projection_delegation_quality_gate`` (migration 0045) are views over
``delegation_events``. A local store had neither, so ``onex dashboard`` listed
both ``degraded`` and the Overview's Run locally, Tier mix and Quality rows
showed Not served. ``SqliteDatabaseAdapter`` now creates both as one store step.

Each test names the failure it exists to catch:

* one recorded delegation still reads 503, or 200 with no row (AC1);
* the local-call share or the tier split disagrees with the rows behind it, or
  a row with no tier is counted as a tier instead of not tier-routed;
* a boolean in the JSON comes back as 0/1 where Postgres serves true/false;
* the median is not percentile_cont(0.5): wrong for an even count, or not
  NULL when no row has a non-zero token count;
* the quality counts treat a row with no verdict as failed;
* the views count or serve another tenant's rows;
* a store created before the step never gains the views.

Every store is a SQLite file under pytest's ``tmp_path``. Reads go through the
real read node over :class:`SqliteTableRowSource`, as ``onex dashboard`` does.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.model_projection_read import ModelProjectionReadRequest
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

_ROUTING = "onex.snapshot.projection.delegation.model-routing.v1"
_QUALITY = "onex.snapshot.projection.delegation.quality-gate.v1"
_TENANT = "0af28cd0-0654-42e0-a050-358ee8240940"
_OTHER = "11111111-2222-4333-8444-555555555555"

# The two exposures as node_projection_delegation's own contract declares them,
# parsed by the same section parser the topic map uses. Discovering every
# contract instead takes seconds, and dod-verify gives this file 30. The full
# map differs only in relation_schema and the source label, which a SQLite read
# does not use.
_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_projection_delegation/contract.yaml"
)
_TOPICS = {
    cfg.topic: cfg
    for cfg in load_projection_exposures_from_contract(
        yaml.safe_load(_CONTRACT.read_text(encoding="utf-8")),
        "node_projection_delegation",
        _CONTRACT,
    )
}


def _delegation(correlation_id: str, **fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "correlation_id": correlation_id,
        "tenant_id": _TENANT,
        "task_type": "reasoning",
        "delegated_to": "qwen-local",
        "cost_tier_name": "local",
        "quality_gate_passed": True,
        "latency_ms": 900,
        "tokens_to_compliance": 120,
        "actual_score": 0.9,
        "required_bar": 0.7,
        "data_source": "real",
        "created_at": "2026-10-08T03:11:00.500000+00:00",
    }
    row.update(fields)
    return row


def _store(tmp_path: Path, *rows: dict[str, Any]) -> Path:
    path = tmp_path / "delegation.sqlite"
    adapter = SqliteDatabaseAdapter(path)
    for row in rows:
        adapter.upsert("delegation_events", "correlation_id", row)
    return path


def _read(path: Path, topic: str, tenant: str = _TENANT) -> tuple[int, dict[str, Any]]:
    handler = HandlerProjectionRead(
        topic_map=_TOPICS, row_source=SqliteTableRowSource(path)
    )
    result = asyncio.run(
        handler.handle(ModelProjectionReadRequest(topic=topic, tenant_id=tenant))
    )
    return result.http_status, dict(result.response or {})


def _row(path: Path, topic: str, tenant: str = _TENANT) -> dict[str, Any]:
    status, body = _read(path, topic, tenant)
    assert status == 200, body
    assert body["row_count"] == 1, body
    row: dict[str, Any] = body["rows"][0]
    return row


@pytest.mark.parametrize("topic", [_ROUTING, _QUALITY])
def test_one_recorded_delegation_reads_200_with_one_row(
    tmp_path: Path, topic: str
) -> None:
    row = _row(_store(tmp_path, _delegation("b4030008")), topic)
    assert row["tenant_id"] == _TENANT


def test_the_local_share_and_tier_mix_match_the_rows(tmp_path: Path) -> None:
    path = _store(
        tmp_path,
        _delegation("l1"),
        _delegation("l2"),
        _delegation("c1", cost_tier_name="cloud", delegated_to="frontier"),
        _delegation("n1", cost_tier_name=None, delegated_to="frontier"),
    )
    by_tier = _row(path, _ROUTING)["by_tier"]
    assert by_tier["local_call_share"] == pytest.approx(0.5)
    assert by_tier["local_call_count"] == 2
    assert by_tier["tier_routed_total"] == 3
    assert by_tier["not_tier_routed_count"] == 1
    assert [
        (tier["cost_tier_name"], tier["count"], tier["tier_routed"])
        for tier in by_tier["tiers"]
    ] == [("local", 2, True), ("cloud", 1, True), ("not_tier_routed", 1, False)]
    assert by_tier["tiers"][0]["pct_of_tier_routed"] == pytest.approx(2 / 3)


def test_the_routing_breakdowns_carry_postgres_shaped_values(tmp_path: Path) -> None:
    path = _store(
        tmp_path,
        _delegation("a", task_type="document", quality_gate_passed=False),
        _delegation("b", task_type="reasoning", created_at="2026-10-08T03:12:00+00:00"),
        _delegation("c", task_type="reasoning", quality_gate_passed=None),
    )
    row = _row(path, _ROUTING)
    assert row["total_delegations"] == 3
    model = row["by_model"][0]
    assert model["model_name"] == "qwen-local"
    assert model["top_task_type"] == "reasoning"
    assert model["task_types"] == ["document", "reasoning"]
    assert model["qg_pass_rate"] == pytest.approx(0.5)
    traces = row["decision_traces"]
    assert traces[0]["correlation_id"] == "b"
    assert {trace["quality_gate_passed"] for trace in traces} == {True, False, None}
    assert traces[0]["created_at"] == pytest.approx(1791429120.0)


@pytest.mark.parametrize(
    ("tokens", "median"),
    [((100, 300, 200), 200.0), ((100, 200, 300, 400), 250.0), ((0, None), None)],
)
def test_the_median_is_percentile_cont_over_non_zero_counts(
    tmp_path: Path, tokens: tuple[int | None, ...], median: float | None
) -> None:
    path = _store(
        tmp_path,
        *(
            _delegation(f"t{index}", tokens_to_compliance=value)
            for index, value in enumerate(tokens)
        ),
    )
    assert _row(path, _QUALITY)["median_tokens_to_compliance"] == (
        median if median is None else pytest.approx(median)
    )


def test_a_row_with_no_verdict_is_not_counted_as_failed(tmp_path: Path) -> None:
    path = _store(
        tmp_path,
        _delegation("p", quality_gate_passed=True),
        _delegation("f", quality_gate_passed=False, quality_gate_detail="below_bar"),
        _delegation("u", quality_gate_passed=None),
    )
    row = _row(path, _QUALITY)
    assert (row["total_passed"], row["total_failed"], row["total_checks"]) == (1, 1, 2)
    assert row["overall_pass_rate"] == pytest.approx(0.5)
    assert row["failure_categories"] == [
        {"category": "below_bar", "count": 1, "pct_of_failures": 1.0}
    ]
    assert row["by_check_type"][0]["check_type"] == "score_vs_required_bar"


@pytest.mark.parametrize("topic", [_ROUTING, _QUALITY])
def test_each_tenant_reads_only_its_own_rows(tmp_path: Path, topic: str) -> None:
    path = _store(
        tmp_path,
        _delegation("a"),
        _delegation("b"),
        _delegation("c", tenant_id=_OTHER),
    )
    count = "total_delegations" if topic == _ROUTING else "total_checks"
    assert _row(path, topic)[count] == 2
    assert _row(path, topic, _OTHER)[count] == 1


def test_a_store_created_before_the_step_gains_the_views_on_its_next_open(
    tmp_path: Path,
) -> None:
    path = _store(tmp_path, _delegation("old"))
    conn = sqlite3.connect(path)
    conn.execute("DROP VIEW projection_delegation_model_routing")
    conn.execute("DROP VIEW projection_delegation_quality_gate")
    conn.execute(
        "DELETE FROM omnimarket_sqlite_store_steps "
        "WHERE step = 'omn20754_delegation_routing_quality_views'"
    )
    conn.commit()
    conn.close()
    assert _read(path, _ROUTING)[0] == 503

    SqliteDatabaseAdapter(path)._connect().close()

    assert _row(path, _ROUTING)["total_delegations"] == 1
    assert _row(path, _QUALITY)["total_checks"] == 1
