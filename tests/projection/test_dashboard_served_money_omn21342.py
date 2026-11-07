# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dashboard API serves its money figures already computed (OMN-21342).

The dashboard used to add and divide served money itself: the Overview window
sum, Spend by model, route spend and cost per run. These tests pin the API that
replaces that arithmetic:

* every run read from a ``delegation_events`` exposure is served a
  manifest-priced cost (``run_manifest_costs``, keyed by correlation_id, so the
  rows keep exactly their declared columns) (pricing manifest price x recorded tokens for the model that served the
  final answer) BESIDE the recorded ``cost_usd``, which is unchanged; a model
  with no manifest row reads null with a reason, never a guessed number;
* ``GET /money/window`` serves the window totals, per-model spend, per-route
  spend and cost per run for the install's tenant, and refuses another tenant
  with 422 as ``/projection`` does.

The store is a real SQLite file read through the real ``onex dashboard`` app and
the contract-declared topic map.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from omnibase_infra.models.pricing.model_pricing_table import ModelPricingTable

from omnimarket.nodes.node_local_dashboard_serve_effect.handlers.handler_local_dashboard_serve import (
    HandlerLocalDashboardServe,
    create_dashboard_app,
)
from omnimarket.nodes.node_local_dashboard_serve_effect.models import (
    ModelLocalDashboardServeRequest,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

_TENANT = "bc08ff01-9d50-442c-aca1-7ad2e687db70"
_DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"
_PRICED = "6a0b5a8c-1d1c-4c43-9a7e-6b0f3f0c2a01"
_ESCALATED = "0f9f7d2e-4b8a-4d51-9a0e-0c4e5b7d8a02"
_UNPRICED = "3c2d1e0f-5a6b-4c7d-8e9f-0a1b2c3d4e03"
_NO_TOKENS = "9e8d7c6b-5a4f-4e3d-2c1b-0a9f8e7d6c04"

# Built once: discovering every contract's exposures takes seconds.
_TOPICS = build_projection_topic_map()
_MANIFEST = ModelPricingTable.from_yaml()


def _price(model: str, tokens_in: int, tokens_out: int) -> str:
    entry = _MANIFEST.get_entry(model)
    assert entry is not None, f"{model} must have a manifest row for this test"
    cost = (
        Decimal(tokens_in) * Decimal(str(entry.input_cost_per_1k)) / 1000
        + Decimal(tokens_out) * Decimal(str(entry.output_cost_per_1k)) / 1000
    )
    return format(cost, "f")


def _delegation_row(
    correlation_id: str,
    *,
    delegated_to: str,
    routed_model: str | None,
    tokens_in: int,
    tokens_out: int,
    cost: float | None,
    escalations: int = 0,
) -> dict[str, object]:
    stamp = "2026-11-02T10:00:00+00:00"
    return {
        "correlation_id": correlation_id,
        "tenant_id": _TENANT,
        "task_type": "document",
        "delegated_to": delegated_to,
        "routed_model": routed_model,
        "tokens_input": tokens_in,
        "tokens_output": tokens_out,
        "escalation_count": escalations,
        "quality_gate_passed": True,
        "terminal_ok": True,
        "cost_usd": cost,
        "data_source": "real",
        "created_at": stamp,
        "written_at": stamp,
    }


def _store(tmp_path: Path) -> Path:
    path = tmp_path / "delegation.sqlite"
    adapter = SqliteDatabaseAdapter(path)
    for row in (
        _delegation_row(
            _PRICED,
            delegated_to="gemini-2.5-flash",
            routed_model="gemini-2.5-flash",
            tokens_in=1200,
            tokens_out=300,
            cost=0.0042,
        ),
        # Escalated: routed first to a local model, answered by Sonnet 5.5. The
        # served model (routed_model) is the one priced.
        _delegation_row(
            _ESCALATED,
            delegated_to="qwen2.5-7b",
            routed_model="claude-sonnet-5-5",
            tokens_in=2000,
            tokens_out=500,
            cost=0.0,
            escalations=1,
        ),
        _delegation_row(
            _UNPRICED,
            delegated_to="my-byok-model-without-a-row",
            routed_model=None,
            tokens_in=100,
            tokens_out=10,
            cost=0.0001,
        ),
        _delegation_row(
            _NO_TOKENS,
            delegated_to="gemini-2.5-flash",
            routed_model=None,
            tokens_in=0,
            tokens_out=0,
            cost=None,
        ),
    ):
        adapter.upsert("delegation_events", "correlation_id", row)
    for kind, start, spend, counterfactual, savings, runs, version in (
        # The all-time row names the current baseline; day rows add into windows.
        ("all", "", "4.00", "10.00", "6.00", 9, "1.0.0"),
        ("day", "2026-11-01", "1.10", "3.00", "1.90", 3, "1.0.0"),
        ("day", "2026-11-02", "0.25", "1.05", "0.80", 2, "1.0.0"),
        ("day", "2026-10-20", "2.65", "5.95", "3.30", 4, "0.9.0"),
    ):
        adapter.upsert(
            "metering_summary",
            "tenant_id,window_kind,window_start,baseline_model",
            {
                "tenant_id": _TENANT,
                "window_kind": kind,
                "window_start": start,
                "window_end": start,
                "as_of": "2026-11-03T09:00:00+00:00",
                "baseline_model": "claude-sonnet-5-5",
                "pricing_manifest_version": version,
                "baseline_state": "resolved",
                "runs_total": runs,
                "runs_measured": runs,
                "runs_unknown_tokens": 0,
                "runs_unknown_spend": 0,
                "tokens_in": 1000 * runs,
                "tokens_out": 100 * runs,
                "spend_usd": spend,
                "counterfactual_usd": counterfactual,
                "savings_usd": savings,
                "summary_json": "{}",
            },
        )
    for day, model, tier, measured, calls, unmeasured in (
        ("2026-11-01", "gemini-2.5-flash", "cheap_cloud", 0.3, 3, 0),
        ("2026-11-01", "qwen2.5-7b", "local", 0.0, 2, 0),
        ("2026-11-02", "gemini-2.5-flash", "cheap_cloud", 0.1, 2, 1),
        ("2026-11-02", "claude-sonnet-5-5", "claude", None, 1, 1),
        # Outside the window: never added in.
        ("2026-10-20", "gemini-2.5-flash", "cheap_cloud", 9.0, 9, 0),
    ):
        adapter.upsert(
            "usage_by_model_day",
            "tenant_id,usage_day,model_id,cost_tier_name",
            {
                "tenant_id": _TENANT,
                "usage_day": day,
                "model_id": model,
                "cost_tier_name": tier,
                "input_tokens": 100 * calls,
                "output_tokens": 10 * calls,
                "cost_usd": measured or 0.0,
                "measured_cost_usd": measured,
                "unmeasured_call_count": unmeasured,
                "call_count": calls,
                "updated_at": "2026-11-03T09:00:00+00:00",
            },
        )
    return path


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    source = SqliteTableRowSource(_store(tmp_path))
    return TestClient(
        create_dashboard_app(
            handler=HandlerProjectionRead(topic_map=_TOPICS, row_source=source),
            tenant=_TENANT,
            topic_map=_TOPICS,
        )
    )


def _runs(client: TestClient) -> dict[str, dict[str, object]]:
    """Each run row merged with its served manifest cost, by correlation_id."""
    response = client.get(f"/projection/{_DECISIONS}?tenant={quote(_TENANT)}")
    assert response.status_code == 200, response.text
    body = response.json()
    costs = body["run_manifest_costs"]
    return {
        str(row["correlation_id"]): {**row, **costs[str(row["correlation_id"])]}
        for row in body["rows"]
    }


def test_a_run_carries_a_manifest_priced_cost_beside_its_recorded_spend(
    client: TestClient,
) -> None:
    row = _runs(client)[_PRICED]
    assert row["manifest_cost_usd"] == _price("gemini-2.5-flash", 1200, 300)
    assert row["manifest_cost_model"] == "gemini-2.5-flash"
    assert row["manifest_cost_pricing_manifest_version"] == _MANIFEST.schema_version
    assert row["manifest_cost_reason"] == "priced"
    # The recorded tier/BYOK spend is served unchanged beside it.
    assert row["cost_usd"] == pytest.approx(0.0042)


def test_an_escalated_run_is_priced_on_the_model_that_served_the_answer(
    client: TestClient,
) -> None:
    row = _runs(client)[_ESCALATED]
    assert row["manifest_cost_model"] == "claude-sonnet-5-5"
    assert row["manifest_cost_usd"] == _price("claude-sonnet-5-5", 2000, 500)
    assert row["cost_usd"] == pytest.approx(0.0)


def test_a_model_with_no_manifest_row_reads_null_with_a_reason(
    client: TestClient,
) -> None:
    row = _runs(client)[_UNPRICED]
    assert row["manifest_cost_usd"] is None
    assert row["manifest_cost_model"] == "my-byok-model-without-a-row"
    assert row["manifest_cost_reason"] == "model_not_in_manifest"
    assert row["cost_usd"] == pytest.approx(0.0001)


def test_a_run_with_no_recorded_tokens_is_not_priced(client: TestClient) -> None:
    row = _runs(client)[_NO_TOKENS]
    assert row["manifest_cost_usd"] is None
    assert row["manifest_cost_reason"] == "tokens_not_recorded"


def _money(client: TestClient, start: str, end: str) -> dict[str, object]:
    response = client.get(f"/money/window?start={start}&end={end}")
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


def test_the_window_totals_are_served_as_exact_decimals(client: TestClient) -> None:
    body = _money(client, "2026-11-01", "2026-11-02")
    assert body["tenant"] == _TENANT
    totals = body["window_totals"]
    assert isinstance(totals, dict)
    assert totals["spend_usd"] == "1.35"
    assert totals["savings_usd"] == "2.70"
    assert totals["counterfactual_usd"] == "4.05"
    assert totals["runs_total"] == 5
    assert totals["tokens_in"] == 5000
    assert totals["days_without_runs"] == 0
    assert totals["pricing_manifest_version"] == "1.0.0"
    assert totals["baseline_model"] == "claude-sonnet-5-5"


def test_a_window_with_days_without_runs_counts_them_and_mixes_versions(
    client: TestClient,
) -> None:
    totals = _money(client, "2026-10-20", "2026-11-02")["window_totals"]
    assert isinstance(totals, dict)
    assert totals["spend_usd"] == "4.00"
    assert totals["days_without_runs"] == 11
    assert totals["pricing_manifest_version"] == "mixed basis"


def test_a_window_with_no_day_rows_ran_nothing_and_cost_nothing(
    client: TestClient,
) -> None:
    totals = _money(client, "2026-09-01", "2026-09-03")["window_totals"]
    assert isinstance(totals, dict)
    assert totals["spend_usd"] == "0"
    assert totals["runs_total"] == 0
    assert totals["days_without_runs"] == 3


def test_spend_per_model_per_route_and_cost_per_run_are_served(
    client: TestClient,
) -> None:
    usage = _money(client, "2026-11-01", "2026-11-02")["usage"]
    assert isinstance(usage, dict)
    models = {m["model"]: m for m in usage["models"]}
    assert models["gemini-2.5-flash"]["measured_cost_usd"] == "0.4"
    assert models["gemini-2.5-flash"]["calls"] == 5
    assert models["gemini-2.5-flash"]["measured_calls"] == 4
    assert models["gemini-2.5-flash"]["unmeasured_calls"] == 1
    assert models["qwen2.5-7b"]["measured_cost_usd"] == "0"
    # Nothing measured is never served as $0.
    assert models["claude-sonnet-5-5"]["measured_cost_usd"] is None
    assert usage["model_spend_total_usd"] == "0.4"
    routes = {r["route"]: r for r in usage["routes"]}
    assert routes["cheap_cloud"]["measured_cost_usd"] == "0.4"
    assert routes["local"]["measured_cost_usd"] == "0"
    assert routes["claude"]["measured_cost_usd"] is None
    assert routes["claude"]["unmeasured_calls"] == 1
    # 0.4 measured dollars over 6 measured calls (8 calls, 2 unmeasured).
    assert Decimal(str(usage["cost_per_run_usd"])) == pytest.approx(
        Decimal("0.4") / 6, abs=Decimal("1e-9")
    )
    days = {d["day"]: d for d in usage["days"]}
    assert days["2026-11-01"]["measured_cost_usd"] == "0.3"
    assert Decimal(str(days["2026-11-01"]["cost_per_run_usd"])) == Decimal("0.06")
    assert days["2026-11-02"]["measured_calls"] == 1


def test_another_tenant_is_refused_and_a_bad_window_is_refused(
    client: TestClient,
) -> None:
    conflict = client.get(
        "/money/window?start=2026-11-01&end=2026-11-02&tenant=someone-else"
    )
    assert conflict.status_code == 422
    assert conflict.json()["error"] == "tenant_conflict"
    backwards = client.get("/money/window?start=2026-11-02&end=2026-11-01")
    assert backwards.status_code == 422
    assert backwards.json()["error"] == "invalid_window"
    malformed = client.get("/money/window?start=yesterday&end=2026-11-01")
    assert malformed.status_code == 422


def test_an_install_with_no_tenant_refuses_the_money_read(tmp_path: Path) -> None:
    source = SqliteTableRowSource(_store(tmp_path))
    client = TestClient(
        create_dashboard_app(
            handler=HandlerProjectionRead(topic_map=_TOPICS, row_source=source),
            tenant=None,
            topic_map=_TOPICS,
        )
    )
    response = client.get("/money/window?start=2026-11-01&end=2026-11-02")
    assert response.status_code == 422
    assert response.json()["error"] == "tenant_not_configured"


def test_deploy_dashboard_money_over_live_http(tmp_path: Path) -> None:
    """Exercise the serving handler, its token guard and the real SQLite reader."""
    (tmp_path / "index.html").write_text("<html>dashboard test pages</html>")
    source = SqliteTableRowSource(_store(tmp_path))
    # This synthetic token belongs only to the test's short-lived server.
    token = "dashboard-money-test-token"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

        def read(path: str, *, authenticated: bool = True) -> dict[str, object]:
            headers = {"Authorization": f"Bearer {token}"} if authenticated else {}
            request = Request(f"http://127.0.0.1:{port}{path}", headers=headers)
            with urlopen(request, timeout=5) as response:
                assert response.status == 200
                body: dict[str, object] = json.load(response)
                return body

        async def serve(app: FastAPI, host: str, bound_port: int) -> None:
            assert host == "127.0.0.1"
            assert bound_port == port
            server = uvicorn.Server(
                uvicorn.Config(app, host=host, port=port, log_level="warning")
            )
            task = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                for _ in range(500):
                    if server.started or task.done():
                        break
                    await asyncio.sleep(0.01)
                assert server.started, "the dashboard HTTP listener did not start"
                window_path = "/money/window?start=2026-11-01&end=2026-11-02"
                with pytest.raises(HTTPError) as denied:
                    await asyncio.to_thread(read, window_path, authenticated=False)
                assert denied.value.code == 401
                assert json.load(denied.value)["error"] == "token_required"
                money = await asyncio.to_thread(read, window_path)
                assert money["tenant"] == _TENANT
                totals = money["window_totals"]
                assert isinstance(totals, dict)
                assert totals["spend_usd"] == "1.35"
                assert totals["savings_usd"] == "2.70"
                assert totals["runs_total"] == 5
                runs = await asyncio.to_thread(read, f"/projection/{_DECISIONS}")
                costs = runs["run_manifest_costs"]
                assert isinstance(costs, dict)
                assert costs[_PRICED]["manifest_cost_usd"] == _price(
                    "gemini-2.5-flash", 1200, 300
                )
                assert costs[_ESCALATED]["manifest_cost_usd"] == _price(
                    "claude-sonnet-5-5", 2000, 500
                )
                with pytest.raises(HTTPError) as conflict:
                    await asyncio.to_thread(read, window_path + "&tenant=someone-else")
                assert conflict.value.code == 422
                assert json.load(conflict.value)["error"] == "tenant_conflict"
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, timeout=5)

        handler = HandlerLocalDashboardServe(
            topic_map=_TOPICS, row_source=source, serve=serve, pages=tmp_path
        )
        result = asyncio.run(
            handler.handle(
                ModelLocalDashboardServeRequest(
                    host="127.0.0.1", port=port, tenant_id=_TENANT, access_token=token
                )
            )
        )
        assert result.tenant_id == _TENANT


if __name__ == "__main__":
    # The contract's curl probe owns this process and its temporary seeded store.
    probe_dir = Path(sys.argv[1])
    (probe_dir / "index.html").write_text("<html>dashboard test pages</html>")
    with socket.socket() as probe_listener:
        probe_listener.bind(("127.0.0.1", 0))
        probe_port = probe_listener.getsockname()[1]
    (probe_dir / "port").write_text(str(probe_port))
    asyncio.run(
        HandlerLocalDashboardServe(
            topic_map=_TOPICS,
            row_source=SqliteTableRowSource(_store(probe_dir)),
            pages=probe_dir,
        ).handle(
            ModelLocalDashboardServeRequest(
                host="127.0.0.1",
                port=probe_port,
                tenant_id=_TENANT,
                access_token="dashboard-money-test-token",
            )
        )
    )
