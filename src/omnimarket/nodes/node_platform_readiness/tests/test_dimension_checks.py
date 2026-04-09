# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for 7 parallel dimension check functions — OMN-8138."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnimarket.nodes.node_platform_readiness.handlers.dimension_checks import (
    CheckContext,
    check_ci_health,
    check_contract_completeness,
    check_cost_measurement,
    check_dashboard_data,
    check_data_flow,
    check_golden_chain,
    check_runtime_wiring,
    run_all_dimensions,
)
from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
    EnumReadinessStatus,
)
from omnimarket.nodes.node_platform_readiness.models.dimension_result_v2 import (
    ModelDimensionResultV2,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_omni_home(tmp_path: Path) -> Path:
    """Temporary OMNI_HOME with .onex_state structure."""
    return tmp_path


@pytest.fixture
def ctx(tmp_omni_home: Path) -> CheckContext:
    return CheckContext(
        omni_home=tmp_omni_home,
        runtime_api="http://test-runtime:8080",
        dashboard_api="http://test-dashboard:3000",
        github_token="test-token",
        github_repos=["OmniNode-ai/omnimarket"],
        http_timeout=5.0,
    )


# ---------------------------------------------------------------------------
# check_contract_completeness
# ---------------------------------------------------------------------------


def test_contract_completeness_missing_dir(ctx: CheckContext) -> None:
    """WARN when sweep directory doesn't exist."""
    result = asyncio.get_event_loop().run_until_complete(
        check_contract_completeness(ctx)
    )
    assert result.status == EnumReadinessStatus.WARN
    assert result.dimension == "contract_completeness"
    assert any("not found" in a for a in result.actionable_items)


def test_contract_completeness_pass(ctx: CheckContext, tmp_omni_home: Path) -> None:
    """PASS when summary shows 0 missing fields."""
    sweep_dir = tmp_omni_home / ".onex_state" / "contract-sweep" / "20260409-120000"
    sweep_dir.mkdir(parents=True)
    summary = {"total_contracts": 42, "missing_required_fields": []}
    (sweep_dir / "summary.json").write_text(json.dumps(summary))

    result = asyncio.get_event_loop().run_until_complete(
        check_contract_completeness(ctx)
    )
    assert result.status == EnumReadinessStatus.PASS
    assert result.check_count == 42
    assert result.actionable_items == []


def test_contract_completeness_fail_with_missing_fields(
    ctx: CheckContext, tmp_omni_home: Path
) -> None:
    """FAIL when summary shows missing fields."""
    sweep_dir = tmp_omni_home / ".onex_state" / "contract-sweep" / "20260409-120000"
    sweep_dir.mkdir(parents=True)
    summary = {
        "total_contracts": 10,
        "missing_required_fields": ["node_foo", "node_bar"],
    }
    (sweep_dir / "summary.json").write_text(json.dumps(summary))

    result = asyncio.get_event_loop().run_until_complete(
        check_contract_completeness(ctx)
    )
    assert result.status == EnumReadinessStatus.FAIL
    assert len(result.actionable_items) == 2


# ---------------------------------------------------------------------------
# check_golden_chain
# ---------------------------------------------------------------------------


def test_golden_chain_missing_dir(ctx: CheckContext) -> None:
    """WARN when golden chain sweep dir doesn't exist."""
    result = asyncio.get_event_loop().run_until_complete(check_golden_chain(ctx))
    assert result.status == EnumReadinessStatus.WARN
    assert result.dimension == "golden_chain"


def test_golden_chain_stale(ctx: CheckContext, tmp_omni_home: Path) -> None:
    """WARN when sweep is older than 4h."""
    sweep_dir = tmp_omni_home / ".onex_state" / "golden-chain-sweep" / "20260409-060000"
    sweep_dir.mkdir(parents=True)
    # Set mtime to 5 hours ago
    old_time = time.time() - (5 * 3600)
    import os

    os.utime(sweep_dir, (old_time, old_time))

    result = asyncio.get_event_loop().run_until_complete(check_golden_chain(ctx))
    assert result.status == EnumReadinessStatus.WARN
    assert any("old" in a for a in result.actionable_items)


def test_golden_chain_pass(ctx: CheckContext, tmp_omni_home: Path) -> None:
    """PASS with fresh sweep and no failures."""
    sweep_dir = tmp_omni_home / ".onex_state" / "golden-chain-sweep" / "20260409-120000"
    sweep_dir.mkdir(parents=True)
    (sweep_dir / "result_node_foo.json").write_text(json.dumps({"passed": True}))
    (sweep_dir / "result_node_bar.json").write_text(json.dumps({"passed": True}))

    result = asyncio.get_event_loop().run_until_complete(check_golden_chain(ctx))
    assert result.status == EnumReadinessStatus.PASS
    assert result.check_count == 2


# ---------------------------------------------------------------------------
# check_data_flow
# ---------------------------------------------------------------------------


def test_data_flow_missing_dir(ctx: CheckContext) -> None:
    """WARN when data-flow directory doesn't exist."""
    result = asyncio.get_event_loop().run_until_complete(check_data_flow(ctx))
    assert result.status == EnumReadinessStatus.WARN
    assert result.dimension == "data_flow"


def test_data_flow_no_major_gaps(ctx: CheckContext, tmp_omni_home: Path) -> None:
    """PASS when no MAJOR gaps."""
    flow_dir = tmp_omni_home / ".onex_state" / "data-flow"
    flow_dir.mkdir(parents=True)
    (flow_dir / "result.json").write_text(
        json.dumps({"total_topics": 15, "major_gaps": []})
    )

    result = asyncio.get_event_loop().run_until_complete(check_data_flow(ctx))
    assert result.status == EnumReadinessStatus.PASS
    assert result.check_count == 15


def test_data_flow_major_gaps(ctx: CheckContext, tmp_omni_home: Path) -> None:
    """FAIL when MAJOR gaps exist."""
    flow_dir = tmp_omni_home / ".onex_state" / "data-flow"
    flow_dir.mkdir(parents=True)
    (flow_dir / "result.json").write_text(
        json.dumps({"total_topics": 10, "major_gaps": ["onex.evt.foo", "onex.evt.bar"]})
    )

    result = asyncio.get_event_loop().run_until_complete(check_data_flow(ctx))
    assert result.status == EnumReadinessStatus.FAIL
    assert len(result.actionable_items) == 2


# ---------------------------------------------------------------------------
# check_runtime_wiring (mocked HTTP)
# ---------------------------------------------------------------------------


def _make_mock_response(status: int, json_data: object) -> MagicMock:
    """Build a mock aiohttp response context manager."""
    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=json_data)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_mock_session(get_cm: MagicMock) -> MagicMock:
    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=get_cm)
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    return session_cm


def test_runtime_wiring_pass(ctx: CheckContext) -> None:
    """PASS when node count >= 40."""
    nodes = [{"id": f"node_{i}"} for i in range(45)]
    session_cm = _make_mock_session(_make_mock_response(200, nodes))

    with patch(
        "omnimarket.nodes.node_platform_readiness.handlers.dimension_checks.aiohttp.ClientSession",
        return_value=session_cm,
    ):
        result = asyncio.get_event_loop().run_until_complete(check_runtime_wiring(ctx))

    assert result.status == EnumReadinessStatus.PASS
    assert result.check_count == 45


def test_runtime_wiring_warn_low_count(ctx: CheckContext) -> None:
    """WARN when fewer than 40 nodes registered."""
    nodes = [{"id": f"node_{i}"} for i in range(10)]
    session_cm = _make_mock_session(_make_mock_response(200, nodes))

    with patch(
        "omnimarket.nodes.node_platform_readiness.handlers.dimension_checks.aiohttp.ClientSession",
        return_value=session_cm,
    ):
        result = asyncio.get_event_loop().run_until_complete(check_runtime_wiring(ctx))

    assert result.status == EnumReadinessStatus.WARN
    assert any("10" in a for a in result.actionable_items)


def test_runtime_wiring_fail_on_http_error(ctx: CheckContext) -> None:
    """FAIL when API returns non-200."""
    session_cm = _make_mock_session(_make_mock_response(500, {}))

    with patch(
        "omnimarket.nodes.node_platform_readiness.handlers.dimension_checks.aiohttp.ClientSession",
        return_value=session_cm,
    ):
        result = asyncio.get_event_loop().run_until_complete(check_runtime_wiring(ctx))

    assert result.status == EnumReadinessStatus.FAIL


def test_runtime_wiring_fail_on_exception(ctx: CheckContext) -> None:
    """FAIL (not crash) when HTTP raises exception."""
    with patch(
        "omnimarket.nodes.node_platform_readiness.handlers.dimension_checks.aiohttp.ClientSession",
        side_effect=Exception("connection refused"),
    ):
        result = asyncio.get_event_loop().run_until_complete(check_runtime_wiring(ctx))

    assert result.status == EnumReadinessStatus.FAIL
    assert result.dimension == "runtime_wiring"


# ---------------------------------------------------------------------------
# check_dashboard_data (mocked HTTP)
# ---------------------------------------------------------------------------


def test_dashboard_data_pass(ctx: CheckContext) -> None:
    """PASS when savings data is non-null and recent."""
    session_cm = _make_mock_session(
        _make_mock_response(200, {"total_savings_usd": 1234.56, "records_last_24h": 42})
    )
    with patch(
        "omnimarket.nodes.node_platform_readiness.handlers.dimension_checks.aiohttp.ClientSession",
        return_value=session_cm,
    ):
        result = asyncio.get_event_loop().run_until_complete(check_dashboard_data(ctx))

    assert result.status == EnumReadinessStatus.PASS
    assert result.check_count == 42


def test_dashboard_data_warn_no_recent(ctx: CheckContext) -> None:
    """WARN when no records in last 24h."""
    session_cm = _make_mock_session(
        _make_mock_response(200, {"total_savings_usd": 1000, "records_last_24h": 0})
    )
    with patch(
        "omnimarket.nodes.node_platform_readiness.handlers.dimension_checks.aiohttp.ClientSession",
        return_value=session_cm,
    ):
        result = asyncio.get_event_loop().run_until_complete(check_dashboard_data(ctx))

    assert result.status == EnumReadinessStatus.WARN


# ---------------------------------------------------------------------------
# check_cost_measurement (mocked HTTP)
# ---------------------------------------------------------------------------


def test_cost_measurement_pass(ctx: CheckContext) -> None:
    """PASS when cost records exist in last 24h."""
    session_cm = _make_mock_session(
        _make_mock_response(200, {"total_cost_usd": 5.67, "records_last_24h": 15})
    )
    with patch(
        "omnimarket.nodes.node_platform_readiness.handlers.dimension_checks.aiohttp.ClientSession",
        return_value=session_cm,
    ):
        result = asyncio.get_event_loop().run_until_complete(
            check_cost_measurement(ctx)
        )

    assert result.status == EnumReadinessStatus.PASS
    assert result.check_count == 15


def test_cost_measurement_warn_no_records(ctx: CheckContext) -> None:
    """WARN when no cost records in last 24h."""
    session_cm = _make_mock_session(
        _make_mock_response(200, {"total_cost_usd": 0, "records_last_24h": 0})
    )
    with patch(
        "omnimarket.nodes.node_platform_readiness.handlers.dimension_checks.aiohttp.ClientSession",
        return_value=session_cm,
    ):
        result = asyncio.get_event_loop().run_until_complete(
            check_cost_measurement(ctx)
        )

    assert result.status == EnumReadinessStatus.WARN
    assert any("projection" in a for a in result.actionable_items)


# ---------------------------------------------------------------------------
# check_ci_health (mocked HTTP)
# ---------------------------------------------------------------------------


def _make_ci_response(check_runs: list[dict[str, object]]) -> MagicMock:
    return _make_mock_response(200, {"check_runs": check_runs})


def test_ci_health_pass(ctx: CheckContext) -> None:
    """PASS when all CI runs succeeded."""
    runs: list[dict[str, object]] = [{"name": "test", "conclusion": "success"}]
    session_cm = _make_mock_session(_make_ci_response(runs))
    with patch(
        "omnimarket.nodes.node_platform_readiness.handlers.dimension_checks.aiohttp.ClientSession",
        return_value=session_cm,
    ):
        result = asyncio.get_event_loop().run_until_complete(check_ci_health(ctx))

    assert result.status == EnumReadinessStatus.PASS
    assert result.valid_zero is True


def test_ci_health_fail_on_failure(ctx: CheckContext) -> None:
    """FAIL when a CI run has failure conclusion."""
    runs: list[dict[str, object]] = [
        {"name": "test", "conclusion": "success"},
        {"name": "lint", "conclusion": "failure"},
    ]
    session_cm = _make_mock_session(_make_ci_response(runs))
    with patch(
        "omnimarket.nodes.node_platform_readiness.handlers.dimension_checks.aiohttp.ClientSession",
        return_value=session_cm,
    ):
        result = asyncio.get_event_loop().run_until_complete(check_ci_health(ctx))

    assert result.status == EnumReadinessStatus.FAIL
    assert any("lint" in a for a in result.actionable_items)


def test_ci_health_valid_zero(ctx: CheckContext) -> None:
    """PASS with valid_zero=True when no CI runs (empty repo/no workflows)."""
    session_cm = _make_mock_session(_make_ci_response([]))
    with patch(
        "omnimarket.nodes.node_platform_readiness.handlers.dimension_checks.aiohttp.ClientSession",
        return_value=session_cm,
    ):
        result = asyncio.get_event_loop().run_until_complete(check_ci_health(ctx))

    assert result.status == EnumReadinessStatus.PASS
    assert result.valid_zero is True


# ---------------------------------------------------------------------------
# run_all_dimensions — parallelism and exception isolation
# ---------------------------------------------------------------------------


def test_run_all_dimensions_returns_7_results(
    ctx: CheckContext, tmp_omni_home: Path
) -> None:
    """Always returns exactly 7 ModelDimensionResultV2 items."""
    nodes = [{"id": f"node_{i}"} for i in range(45)]
    session_cm = _make_mock_session(_make_mock_response(200, nodes))

    with patch(
        "omnimarket.nodes.node_platform_readiness.handlers.dimension_checks.aiohttp.ClientSession",
        return_value=session_cm,
    ):
        results = asyncio.get_event_loop().run_until_complete(run_all_dimensions(ctx))

    assert len(results) == 7
    assert all(isinstance(r, ModelDimensionResultV2) for r in results)


def test_run_all_dimensions_exception_does_not_crash(ctx: CheckContext) -> None:
    """Exception in one dimension produces FAIL for that dimension, others unaffected."""

    # Patch runtime wiring to raise
    async def boom(ctx: CheckContext) -> ModelDimensionResultV2:
        raise RuntimeError("simulated crash")

    with (
        patch(
            "omnimarket.nodes.node_platform_readiness.handlers.dimension_checks.check_runtime_wiring",
            side_effect=boom,
        ),
        patch(
            "omnimarket.nodes.node_platform_readiness.handlers.dimension_checks.aiohttp.ClientSession",
            return_value=_make_mock_session(
                _make_mock_response(
                    200,
                    {
                        "records_last_24h": 5,
                        "total_savings_usd": 10,
                        "total_cost_usd": 1,
                    },
                )
            ),
        ),
    ):
        results = asyncio.get_event_loop().run_until_complete(run_all_dimensions(ctx))

    assert len(results) == 7
    # runtime_wiring is index 3 — should be FAIL
    runtime_result = next(r for r in results if r.dimension == "runtime_wiring")
    assert runtime_result.status == EnumReadinessStatus.FAIL


def test_run_all_dimensions_uses_asyncio_gather(ctx: CheckContext) -> None:
    """Confirm run_all_dimensions runs concurrently (no sequential blocking)."""
    call_times: list[float] = []

    async def slow_check(dimension: str) -> ModelDimensionResultV2:
        call_times.append(time.time())
        await asyncio.sleep(0.05)
        return ModelDimensionResultV2(
            dimension=dimension,
            status=EnumReadinessStatus.PASS,
            check_count=1,
            evidence_source="mock",
        )

    patches = {
        "check_contract_completeness": lambda _ctx: slow_check("contract_completeness"),
        "check_golden_chain": lambda _ctx: slow_check("golden_chain"),
        "check_data_flow": lambda _ctx: slow_check("data_flow"),
        "check_runtime_wiring": lambda _ctx: slow_check("runtime_wiring"),
        "check_dashboard_data": lambda _ctx: slow_check("dashboard_data"),
        "check_cost_measurement": lambda _ctx: slow_check("cost_measurement"),
        "check_ci_health": lambda _ctx: slow_check("ci_health"),
    }

    base = "omnimarket.nodes.node_platform_readiness.handlers.dimension_checks"
    with (
        patch(
            f"{base}.check_contract_completeness",
            patches["check_contract_completeness"],
        ),
        patch(f"{base}.check_golden_chain", patches["check_golden_chain"]),
        patch(f"{base}.check_data_flow", patches["check_data_flow"]),
        patch(f"{base}.check_runtime_wiring", patches["check_runtime_wiring"]),
        patch(f"{base}.check_dashboard_data", patches["check_dashboard_data"]),
        patch(f"{base}.check_cost_measurement", patches["check_cost_measurement"]),
        patch(f"{base}.check_ci_health", patches["check_ci_health"]),
    ):
        start = time.time()
        results = asyncio.get_event_loop().run_until_complete(run_all_dimensions(ctx))
        elapsed = time.time() - start

    assert len(results) == 7
    # If sequential: ~7 * 0.05 = 0.35s. If parallel: ~0.05s + overhead.
    # Allow generous margin but assert well under sequential time.
    assert elapsed < 0.25, (
        f"run_all_dimensions took {elapsed:.3f}s — likely not parallel"
    )
