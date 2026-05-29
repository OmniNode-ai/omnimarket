# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Focused unit tests for dimension_checks.py diagnostic verdicts.

Covers all 7 dimension check functions verifying:
- PASS verdict when data is clean and fresh
- WARN verdict when sweep directory or results are missing
- WARN verdict when sweep artifact is stale
- FAIL verdict when actionable findings exist
- Exception wrapping: any unhandled exception produces FAIL result

Related: OMN-12385
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from omnimarket.nodes.node_platform_diagnostics.handlers.dimension_checks import (
    DiagnosticsCheckContext,
    check_contract_health,
    check_coverage,
    check_database_projections,
    check_golden_chain,
    check_hook_health,
    check_runtime_nodes,
    run_dimension_checks,
)
from omnimarket.nodes.node_platform_diagnostics.models.model_diagnostics_result import (
    EnumDiagnosticDimension,
)
from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
    EnumReadinessStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(tmp_path: Path, *, dry_run: bool = False) -> DiagnosticsCheckContext:
    return DiagnosticsCheckContext(
        omni_home=tmp_path,
        dashboard_api="http://localhost:9999",
        github_token="",
        github_repos=["OmniNode-ai/omnimarket"],
        http_timeout=1.0,
        freshness_threshold_hours=4,
        dry_run=dry_run,
    )


def _write_fresh_summary(
    sweep_dir: Path,
    sub: str,
    data: dict[str, object],
    *,
    use_subdirectory: bool = True,
) -> None:
    """Write a fresh summary.json under sweep_dir/<sub>/ or sweep_dir directly."""
    if use_subdirectory:
        run_dir = sweep_dir / sub
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "summary.json").write_text(json.dumps(data))
    else:
        sweep_dir.mkdir(parents=True, exist_ok=True)
        (sweep_dir / f"{sub}.json").write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# check_contract_health
# ---------------------------------------------------------------------------


class TestCheckContractHealth:
    @pytest.mark.asyncio
    async def test_missing_sweep_dir_returns_warn(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        result = await check_contract_health(ctx)
        assert result.status == EnumReadinessStatus.WARN
        assert result.dimension == EnumDiagnosticDimension.CONTRACT_HEALTH

    @pytest.mark.asyncio
    async def test_no_runs_in_sweep_dir_returns_warn(self, tmp_path: Path) -> None:
        (tmp_path / ".onex_state" / "contract-sweep").mkdir(parents=True)
        ctx = _ctx(tmp_path)
        result = await check_contract_health(ctx)
        assert result.status == EnumReadinessStatus.WARN

    @pytest.mark.asyncio
    async def test_stale_sweep_returns_warn(self, tmp_path: Path) -> None:
        sweep_dir = tmp_path / ".onex_state" / "contract-sweep"
        run_dir = sweep_dir / "2020-01-01_000000"
        run_dir.mkdir(parents=True)
        (run_dir / "summary.json").write_text(
            json.dumps({"missing_required_fields": [], "total_contracts": 5})
        )
        import os

        old_time = time.time() - (5 * 3600)  # 5 hours old, threshold is 4h
        os.utime(run_dir, (old_time, old_time))

        ctx = _ctx(tmp_path)
        result = await check_contract_health(ctx)
        assert result.status == EnumReadinessStatus.WARN
        assert result.freshness_seconds is not None
        assert result.freshness_seconds > 4 * 3600

    @pytest.mark.asyncio
    async def test_clean_fresh_sweep_returns_pass(self, tmp_path: Path) -> None:
        sweep_dir = tmp_path / ".onex_state" / "contract-sweep"
        run_dir = sweep_dir / "2099-01-01_000000"
        run_dir.mkdir(parents=True)
        (run_dir / "summary.json").write_text(
            json.dumps({"missing_required_fields": [], "total_contracts": 10})
        )
        ctx = _ctx(tmp_path)
        result = await check_contract_health(ctx)
        assert result.status == EnumReadinessStatus.PASS
        assert result.check_count == 10

    @pytest.mark.asyncio
    async def test_missing_fields_returns_fail(self, tmp_path: Path) -> None:
        sweep_dir = tmp_path / ".onex_state" / "contract-sweep"
        run_dir = sweep_dir / "2099-01-01_000000"
        run_dir.mkdir(parents=True)
        (run_dir / "summary.json").write_text(
            json.dumps(
                {
                    "missing_required_fields": [
                        "node_a: description",
                        "node_b: version",
                    ],
                    "total_contracts": 10,
                }
            )
        )
        ctx = _ctx(tmp_path)
        result = await check_contract_health(ctx)
        assert result.status == EnumReadinessStatus.FAIL
        assert len(result.actionable_items) >= 2

    @pytest.mark.asyncio
    async def test_exception_wrapped_as_fail(self, tmp_path: Path) -> None:
        """If sweep_dir.iterdir() raises, result is FAIL."""
        ctx = _ctx(tmp_path)
        # Point omni_home at a non-directory to force an OSError on stat
        sweep_dir = tmp_path / ".onex_state" / "contract-sweep"
        sweep_dir.parent.mkdir(parents=True, exist_ok=True)
        sweep_dir.write_text("not a directory")  # file, not dir
        result = await check_contract_health(ctx)
        assert result.status == EnumReadinessStatus.FAIL


# ---------------------------------------------------------------------------
# check_golden_chain
# ---------------------------------------------------------------------------


class TestCheckGoldenChain:
    @pytest.mark.asyncio
    async def test_missing_sweep_dir_returns_warn(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        result = await check_golden_chain(ctx)
        assert result.status == EnumReadinessStatus.WARN
        assert result.dimension == EnumDiagnosticDimension.GOLDEN_CHAIN

    @pytest.mark.asyncio
    async def test_no_runs_returns_warn(self, tmp_path: Path) -> None:
        (tmp_path / ".onex_state" / "golden-chain-sweep").mkdir(parents=True)
        ctx = _ctx(tmp_path)
        result = await check_golden_chain(ctx)
        assert result.status == EnumReadinessStatus.WARN

    @pytest.mark.asyncio
    async def test_all_passed_returns_pass(self, tmp_path: Path) -> None:
        sweep_dir = tmp_path / ".onex_state" / "golden-chain-sweep"
        run_dir = sweep_dir / "run_2099"
        run_dir.mkdir(parents=True)
        (run_dir / "chain_a.json").write_text(json.dumps({"passed": True}))
        (run_dir / "chain_b.json").write_text(json.dumps({"passed": True}))
        ctx = _ctx(tmp_path)
        result = await check_golden_chain(ctx)
        assert result.status == EnumReadinessStatus.PASS
        assert result.check_count == 2

    @pytest.mark.asyncio
    async def test_failed_chain_returns_fail(self, tmp_path: Path) -> None:
        sweep_dir = tmp_path / ".onex_state" / "golden-chain-sweep"
        run_dir = sweep_dir / "run_2099"
        run_dir.mkdir(parents=True)
        (run_dir / "chain_a.json").write_text(json.dumps({"passed": True}))
        (run_dir / "chain_b.json").write_text(json.dumps({"passed": False}))
        ctx = _ctx(tmp_path)
        result = await check_golden_chain(ctx)
        assert result.status == EnumReadinessStatus.FAIL
        assert any("chain_b" in item for item in result.actionable_items)


# ---------------------------------------------------------------------------
# check_runtime_nodes
# ---------------------------------------------------------------------------


class TestCheckRuntimeNodes:
    @pytest.mark.asyncio
    async def test_missing_sweep_dir_returns_warn(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        result = await check_runtime_nodes(ctx)
        assert result.status == EnumReadinessStatus.WARN
        assert result.dimension == EnumDiagnosticDimension.RUNTIME_NODES

    @pytest.mark.asyncio
    async def test_no_missing_nodes_returns_pass(self, tmp_path: Path) -> None:
        sweep_dir = tmp_path / ".onex_state" / "runtime-sweep"
        sweep_dir.mkdir(parents=True)
        (sweep_dir / "result.json").write_text(
            json.dumps({"missing_entry_points": [], "total_nodes": 12})
        )
        ctx = _ctx(tmp_path)
        result = await check_runtime_nodes(ctx)
        assert result.status == EnumReadinessStatus.PASS
        assert result.check_count == 12

    @pytest.mark.asyncio
    async def test_missing_nodes_returns_fail(self, tmp_path: Path) -> None:
        sweep_dir = tmp_path / ".onex_state" / "runtime-sweep"
        sweep_dir.mkdir(parents=True)
        (sweep_dir / "result.json").write_text(
            json.dumps(
                {"missing_entry_points": ["node_foo", "node_bar"], "total_nodes": 10}
            )
        )
        ctx = _ctx(tmp_path)
        result = await check_runtime_nodes(ctx)
        assert result.status == EnumReadinessStatus.FAIL
        assert len(result.actionable_items) >= 2


# ---------------------------------------------------------------------------
# check_hook_health (dry_run path only — no live HTTP)
# ---------------------------------------------------------------------------


class TestCheckHookHealthDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_no_violations_file_returns_warn(
        self, tmp_path: Path
    ) -> None:
        ctx = _ctx(tmp_path, dry_run=True)
        result = await check_hook_health(ctx)
        assert result.status == EnumReadinessStatus.WARN
        assert result.dimension == EnumDiagnosticDimension.HOOK_HEALTH

    @pytest.mark.asyncio
    async def test_dry_run_empty_violations_returns_pass(self, tmp_path: Path) -> None:
        logs_dir = tmp_path / ".onex_state" / "hooks" / "logs"
        logs_dir.mkdir(parents=True)
        (logs_dir / "violations.log").write_text("")
        ctx = _ctx(tmp_path, dry_run=True)
        result = await check_hook_health(ctx)
        assert result.status == EnumReadinessStatus.PASS

    @pytest.mark.asyncio
    async def test_dry_run_with_violations_returns_warn(self, tmp_path: Path) -> None:
        logs_dir = tmp_path / ".onex_state" / "hooks" / "logs"
        logs_dir.mkdir(parents=True)
        (logs_dir / "violations.log").write_text(
            "violation: hardcoded path /Users/foo\n"
            "violation: hardcoded ip 192.168.0.1\n"
        )
        ctx = _ctx(tmp_path, dry_run=True)
        result = await check_hook_health(ctx)
        assert result.status == EnumReadinessStatus.WARN
        assert result.check_count == 2


# ---------------------------------------------------------------------------
# check_database_projections
# ---------------------------------------------------------------------------


class TestCheckDatabaseProjections:
    @pytest.mark.asyncio
    async def test_missing_sweep_dir_returns_warn(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        result = await check_database_projections(ctx)
        assert result.status == EnumReadinessStatus.WARN
        assert result.dimension == EnumDiagnosticDimension.DATABASE_PROJECTIONS

    @pytest.mark.asyncio
    async def test_no_unpopulated_tables_returns_pass(self, tmp_path: Path) -> None:
        sweep_dir = tmp_path / ".onex_state" / "database-sweep"
        sweep_dir.mkdir(parents=True)
        (sweep_dir / "db.json").write_text(
            json.dumps({"unpopulated_tables": [], "total_tables": 8})
        )
        ctx = _ctx(tmp_path)
        result = await check_database_projections(ctx)
        assert result.status == EnumReadinessStatus.PASS

    @pytest.mark.asyncio
    async def test_unpopulated_tables_returns_warn(self, tmp_path: Path) -> None:
        sweep_dir = tmp_path / ".onex_state" / "database-sweep"
        sweep_dir.mkdir(parents=True)
        (sweep_dir / "db.json").write_text(
            json.dumps({"unpopulated_tables": ["projections_foo"], "total_tables": 8})
        )
        ctx = _ctx(tmp_path)
        result = await check_database_projections(ctx)
        assert result.status == EnumReadinessStatus.WARN

    @pytest.mark.asyncio
    async def test_alternate_db_sweep_dir_name(self, tmp_path: Path) -> None:
        """Falls back to db-sweep when database-sweep doesn't exist."""
        sweep_dir = tmp_path / ".onex_state" / "db-sweep"
        sweep_dir.mkdir(parents=True)
        (sweep_dir / "db.json").write_text(
            json.dumps({"unpopulated_tables": [], "total_tables": 3})
        )
        ctx = _ctx(tmp_path)
        result = await check_database_projections(ctx)
        assert result.status == EnumReadinessStatus.PASS


# ---------------------------------------------------------------------------
# check_coverage
# ---------------------------------------------------------------------------


class TestCheckCoverage:
    @pytest.mark.asyncio
    async def test_missing_sweep_dir_returns_warn(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        result = await check_coverage(ctx)
        assert result.status == EnumReadinessStatus.WARN
        assert result.dimension == EnumDiagnosticDimension.COVERAGE

    @pytest.mark.asyncio
    async def test_all_repos_above_threshold_returns_pass(self, tmp_path: Path) -> None:
        sweep_dir = tmp_path / ".onex_state" / "coverage-sweep"
        sweep_dir.mkdir(parents=True)
        (sweep_dir / "cov.json").write_text(
            json.dumps({"below_threshold_repos": [], "total_repos": 5})
        )
        ctx = _ctx(tmp_path)
        result = await check_coverage(ctx)
        assert result.status == EnumReadinessStatus.PASS
        assert result.check_count == 5

    @pytest.mark.asyncio
    async def test_repos_below_threshold_returns_warn(self, tmp_path: Path) -> None:
        sweep_dir = tmp_path / ".onex_state" / "coverage-sweep"
        sweep_dir.mkdir(parents=True)
        (sweep_dir / "cov.json").write_text(
            json.dumps(
                {
                    "below_threshold_repos": ["omnimarket", "omniclaude"],
                    "total_repos": 5,
                }
            )
        )
        ctx = _ctx(tmp_path)
        result = await check_coverage(ctx)
        assert result.status == EnumReadinessStatus.WARN
        assert len(result.actionable_items) == 2


# ---------------------------------------------------------------------------
# run_dimension_checks: orchestration and exception isolation
# ---------------------------------------------------------------------------


class TestRunDimensionChecks:
    @pytest.mark.asyncio
    async def test_runs_all_requested_dimensions(self, tmp_path: Path) -> None:
        """run_dimension_checks gathers results for all requested dimensions."""
        ctx = _ctx(tmp_path)
        dims = [
            EnumDiagnosticDimension.CONTRACT_HEALTH,
            EnumDiagnosticDimension.COVERAGE,
        ]
        results = await run_dimension_checks(ctx, dims)
        assert len(results) == 2
        assert {r.dimension for r in results} == set(dims)

    @pytest.mark.asyncio
    async def test_exception_in_one_check_does_not_propagate(
        self, tmp_path: Path
    ) -> None:
        """An exception in a single check produces a FAIL result, not a crash."""
        from unittest.mock import patch

        async def boom(ctx: DiagnosticsCheckContext) -> None:
            raise RuntimeError("simulated check failure")

        ctx = _ctx(tmp_path)
        with patch.dict(
            "omnimarket.nodes.node_platform_diagnostics.handlers.dimension_checks.DIMENSION_CHECK_MAP",
            {EnumDiagnosticDimension.CONTRACT_HEALTH: boom},
        ):
            results = await run_dimension_checks(
                ctx, [EnumDiagnosticDimension.CONTRACT_HEALTH]
            )
        assert len(results) == 1
        assert results[0].status == EnumReadinessStatus.FAIL
        assert "simulated check failure" in results[0].actionable_items[0]

    @pytest.mark.asyncio
    async def test_empty_dimension_list_returns_empty(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        results = await run_dimension_checks(ctx, [])
        assert results == []
