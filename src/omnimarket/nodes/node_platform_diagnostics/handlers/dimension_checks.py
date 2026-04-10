# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""7 parallel dimension check functions for node_platform_diagnostics.

Each function is `async def check_<dimension>(ctx: DiagnosticsCheckContext) -> ModelDiagnosticDimensionResult`
and runs independently. All are gathered in parallel via asyncio.gather.

Evidence classes:
  - Cached sweep artifact: reads local .onex_state files (medium trust)
  - Live HTTP probe: calls HTTP endpoint in real time (high trust)
  - GitHub API: rate-limited, may be cached (medium trust)

If a check raises an exception it is wrapped in a FAIL result. asyncio.gather never
propagates exceptions up to the orchestrator.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

from omnimarket.nodes.node_platform_diagnostics.models.model_diagnostics_result import (
    EnumDiagnosticDimension,
    ModelDiagnosticDimensionResult,
)
from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
    EnumReadinessStatus,
)

_OMNI_HOME = os.environ.get("OMNI_HOME", os.path.expanduser("~/Code/omni_home"))
_DASHBOARD_API = os.environ.get("ONEX_DASHBOARD_API", "http://192.168.86.201:3000")
_GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
_GITHUB_REPOS = [
    "OmniNode-ai/omnimarket",
    "OmniNode-ai/omniclaude",
    "OmniNode-ai/omnibase_core",
    "OmniNode-ai/omnibase_infra",
    "OmniNode-ai/omnibase_spi",
    "OmniNode-ai/omniintelligence",
    "OmniNode-ai/omnidash",
    "OmniNode-ai/onex_change_control",
]


@dataclass
class DiagnosticsCheckContext:
    """Shared context passed to all dimension check functions."""

    omni_home: Path = field(default_factory=lambda: Path(_OMNI_HOME))
    dashboard_api: str = _DASHBOARD_API
    github_token: str = field(default_factory=lambda: _GITHUB_TOKEN)
    github_repos: list[str] = field(default_factory=lambda: list(_GITHUB_REPOS))
    http_timeout: float = 10.0
    freshness_threshold_hours: int = 4
    dry_run: bool = False


def _wrap_exception(
    dimension: EnumDiagnosticDimension, evidence_source: str, exc: Exception
) -> ModelDiagnosticDimensionResult:
    """Wrap an unhandled exception as a FAIL dimension result."""
    return ModelDiagnosticDimensionResult(
        dimension=dimension,
        status=EnumReadinessStatus.FAIL,
        check_count=0,
        valid_zero=False,
        actionable_items=[f"check raised {type(exc).__name__}: {exc}"],
        evidence_source=evidence_source,
        raw_detail=str(exc),
    )


async def check_contract_health(
    ctx: DiagnosticsCheckContext,
) -> ModelDiagnosticDimensionResult:
    """Check contract completeness from cached contract-sweep artifact.

    Evidence class: Cached sweep artifact (medium trust).
    """
    dimension = EnumDiagnosticDimension.CONTRACT_HEALTH
    evidence_source = "onex_change_control"
    try:
        sweep_dir = ctx.omni_home / ".onex_state" / "contract-sweep"
        if not sweep_dir.exists():
            return ModelDiagnosticDimensionResult(
                dimension=dimension,
                status=EnumReadinessStatus.WARN,
                check_count=0,
                valid_zero=False,
                actionable_items=[
                    "contract sweep directory not found — run /onex:contract_sweep"
                ],
                evidence_source=evidence_source,
            )

        sweep_dirs = sorted(
            [d for d in sweep_dir.iterdir() if d.is_dir()], reverse=True
        )
        if not sweep_dirs:
            return ModelDiagnosticDimensionResult(
                dimension=dimension,
                status=EnumReadinessStatus.WARN,
                check_count=0,
                valid_zero=False,
                actionable_items=[
                    "no contract sweep runs found — run /onex:contract_sweep"
                ],
                evidence_source=evidence_source,
            )

        latest = sweep_dirs[0]
        freshness = int(time.time() - latest.stat().st_mtime)
        threshold_seconds = ctx.freshness_threshold_hours * 3600

        if freshness > threshold_seconds:
            hours = freshness // 3600
            return ModelDiagnosticDimensionResult(
                dimension=dimension,
                status=EnumReadinessStatus.WARN,
                check_count=0,
                valid_zero=False,
                actionable_items=[
                    f"contract sweep is {hours}h old — re-run /onex:contract_sweep"
                ],
                evidence_source=evidence_source,
                freshness_seconds=freshness,
            )

        summary_file = latest / "summary.json"
        if not summary_file.exists():
            summary_file = latest / "results.json"

        if not summary_file.exists():
            return ModelDiagnosticDimensionResult(
                dimension=dimension,
                status=EnumReadinessStatus.WARN,
                check_count=0,
                valid_zero=False,
                actionable_items=[f"no summary.json in {latest.name}"],
                evidence_source=evidence_source,
                freshness_seconds=freshness,
            )

        data: dict[str, Any] = json.loads(summary_file.read_text())
        missing_fields = data.get("missing_required_fields", [])
        total = data.get("total_contracts", 0) or data.get("checked", 0)
        actionable: list[str] = [
            f"contract missing required fields: {m}" for m in missing_fields[:5]
        ]
        if len(missing_fields) > 5:
            actionable.append(f"... and {len(missing_fields) - 5} more")

        status = (
            EnumReadinessStatus.PASS if not missing_fields else EnumReadinessStatus.FAIL
        )

        return ModelDiagnosticDimensionResult(
            dimension=dimension,
            status=status,
            check_count=total,
            valid_zero=False,
            actionable_items=actionable,
            evidence_source=evidence_source,
            freshness_seconds=freshness,
            raw_detail=f"missing_fields={len(missing_fields)} total={total}",
        )
    except Exception as exc:
        return _wrap_exception(dimension, evidence_source, exc)


async def check_golden_chain(
    ctx: DiagnosticsCheckContext,
) -> ModelDiagnosticDimensionResult:
    """Check golden chain sweep result freshness and pass/fail status.

    Evidence class: Cached sweep artifact (medium trust).
    """
    dimension = EnumDiagnosticDimension.GOLDEN_CHAIN
    evidence_source = "golden_chain_sweep_artifact"
    try:
        sweep_dir = ctx.omni_home / ".onex_state" / "golden-chain-sweep"
        if not sweep_dir.exists():
            return ModelDiagnosticDimensionResult(
                dimension=dimension,
                status=EnumReadinessStatus.WARN,
                check_count=0,
                valid_zero=False,
                actionable_items=[
                    "golden chain sweep directory not found — run /onex:golden_chain_sweep"
                ],
                evidence_source=evidence_source,
            )

        sweep_dirs = sorted(
            [d for d in sweep_dir.iterdir() if d.is_dir()], reverse=True
        )
        if not sweep_dirs:
            return ModelDiagnosticDimensionResult(
                dimension=dimension,
                status=EnumReadinessStatus.WARN,
                check_count=0,
                valid_zero=False,
                actionable_items=["no golden chain sweep runs found"],
                evidence_source=evidence_source,
            )

        latest = sweep_dirs[0]
        freshness = int(time.time() - latest.stat().st_mtime)
        threshold_seconds = ctx.freshness_threshold_hours * 3600

        if freshness > threshold_seconds:
            hours = freshness // 3600
            return ModelDiagnosticDimensionResult(
                dimension=dimension,
                status=EnumReadinessStatus.WARN,
                check_count=0,
                valid_zero=False,
                actionable_items=[
                    f"golden chain sweep is {hours}h old — re-run /onex:golden_chain_sweep"
                ],
                evidence_source=evidence_source,
                freshness_seconds=freshness,
            )

        result_files = list(latest.glob("*.json"))
        check_count = len(result_files)
        failures = []
        for rf in result_files:
            try:
                d = json.loads(rf.read_text())
                if not d.get("passed", True):
                    failures.append(rf.stem)
            except Exception:
                pass

        status = EnumReadinessStatus.PASS if not failures else EnumReadinessStatus.FAIL
        actionable = [f"golden chain failed: {f}" for f in failures[:5]]

        return ModelDiagnosticDimensionResult(
            dimension=dimension,
            status=status,
            check_count=check_count,
            valid_zero=True,
            actionable_items=actionable,
            evidence_source=evidence_source,
            freshness_seconds=freshness,
        )
    except Exception as exc:
        return _wrap_exception(dimension, evidence_source, exc)


async def check_runtime_nodes(
    ctx: DiagnosticsCheckContext,
) -> ModelDiagnosticDimensionResult:
    """Check runtime node registration from cached runtime sweep artifact.

    Evidence class: Cached sweep artifact (medium trust).
    """
    dimension = EnumDiagnosticDimension.RUNTIME_NODES
    evidence_source = "runtime_sweep_artifact"
    try:
        sweep_dir = ctx.omni_home / ".onex_state" / "runtime-sweep"
        if not sweep_dir.exists():
            return ModelDiagnosticDimensionResult(
                dimension=dimension,
                status=EnumReadinessStatus.WARN,
                check_count=0,
                valid_zero=False,
                actionable_items=[
                    "runtime sweep directory not found — run /onex:runtime_sweep"
                ],
                evidence_source=evidence_source,
            )

        result_files = sorted(
            sweep_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True
        )
        if not result_files:
            return ModelDiagnosticDimensionResult(
                dimension=dimension,
                status=EnumReadinessStatus.WARN,
                check_count=0,
                valid_zero=False,
                actionable_items=["no runtime sweep results found"],
                evidence_source=evidence_source,
            )

        latest_file = result_files[0]
        freshness = int(time.time() - latest_file.stat().st_mtime)
        threshold_seconds = ctx.freshness_threshold_hours * 3600

        if freshness > threshold_seconds:
            hours = freshness // 3600
            return ModelDiagnosticDimensionResult(
                dimension=dimension,
                status=EnumReadinessStatus.WARN,
                check_count=0,
                valid_zero=False,
                actionable_items=[
                    f"runtime sweep is {hours}h old — re-run /onex:runtime_sweep"
                ],
                evidence_source=evidence_source,
                freshness_seconds=freshness,
            )

        data: dict[str, Any] = json.loads(latest_file.read_text())
        missing_nodes = data.get("missing_entry_points", []) or data.get(
            "unresolvable", []
        )
        node_count = data.get("total_nodes", 0) or data.get("registered", 0)
        actionable = [f"unresolvable node: {n}" for n in missing_nodes[:5]]
        if len(missing_nodes) > 5:
            actionable.append(f"... and {len(missing_nodes) - 5} more")

        status = (
            EnumReadinessStatus.PASS if not missing_nodes else EnumReadinessStatus.FAIL
        )

        return ModelDiagnosticDimensionResult(
            dimension=dimension,
            status=status,
            check_count=node_count,
            valid_zero=False,
            actionable_items=actionable,
            evidence_source=evidence_source,
            freshness_seconds=freshness,
            raw_detail=f"missing={len(missing_nodes)} total={node_count}",
        )
    except Exception as exc:
        return _wrap_exception(dimension, evidence_source, exc)


async def check_hook_health(
    ctx: DiagnosticsCheckContext,
) -> ModelDiagnosticDimensionResult:
    """Live HTTP probe to omnidash GET /api/hook-health/summary.

    Evidence class: Live HTTP probe (high trust).
    In dry_run mode: reads cached .onex_state/hooks/logs/ instead.
    """
    dimension = EnumDiagnosticDimension.HOOK_HEALTH
    evidence_source = "omnidash_hook_health_api"

    if ctx.dry_run:
        # Dry-run: read from local hook logs
        evidence_source = "hooks_log_artifact"
        try:
            violations_file = (
                ctx.omni_home / ".onex_state" / "hooks" / "logs" / "violations.log"
            )
            if not violations_file.exists():
                return ModelDiagnosticDimensionResult(
                    dimension=dimension,
                    status=EnumReadinessStatus.WARN,
                    check_count=0,
                    valid_zero=True,
                    actionable_items=["hook violations log not found"],
                    evidence_source=evidence_source,
                )
            content = violations_file.read_text()
            violation_lines = [line for line in content.splitlines() if line.strip()]
            status = (
                EnumReadinessStatus.PASS
                if not violation_lines
                else EnumReadinessStatus.WARN
            )
            return ModelDiagnosticDimensionResult(
                dimension=dimension,
                status=status,
                check_count=len(violation_lines),
                valid_zero=True,
                actionable_items=(
                    [f"hook violation: {line[:120]}" for line in violation_lines[:3]]
                    if violation_lines
                    else []
                ),
                evidence_source=evidence_source,
                raw_detail=f"violations={len(violation_lines)}",
            )
        except Exception as exc:
            return _wrap_exception(dimension, evidence_source, exc)

    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                f"{ctx.dashboard_api}/api/hook-health/summary",
                timeout=aiohttp.ClientTimeout(total=ctx.http_timeout),
            ) as resp,
        ):
            if resp.status != 200:
                return ModelDiagnosticDimensionResult(
                    dimension=dimension,
                    status=EnumReadinessStatus.FAIL,
                    check_count=0,
                    valid_zero=False,
                    actionable_items=[f"hook health API returned HTTP {resp.status}"],
                    evidence_source=evidence_source,
                    raw_detail=f"GET /api/hook-health/summary → {resp.status}",
                )

            data = await resp.json()
            error_rate = data.get("error_rate_pct", 0)
            total_hooks = data.get("total_hooks", 0)
            actionable: list[str] = []

            if error_rate > 10:
                actionable.append(f"hook error rate {error_rate:.1f}% > 10% threshold")
                status = EnumReadinessStatus.FAIL
            elif error_rate > 5:
                actionable.append(f"hook error rate {error_rate:.1f}% > 5% — degraded")
                status = EnumReadinessStatus.WARN
            else:
                status = EnumReadinessStatus.PASS

            return ModelDiagnosticDimensionResult(
                dimension=dimension,
                status=status,
                check_count=total_hooks,
                valid_zero=True,
                actionable_items=actionable,
                evidence_source=evidence_source,
                raw_detail=f"error_rate={error_rate} total_hooks={total_hooks}",
            )
    except Exception as exc:
        return _wrap_exception(dimension, evidence_source, exc)


async def check_database_projections(
    ctx: DiagnosticsCheckContext,
) -> ModelDiagnosticDimensionResult:
    """Check database projection health from cached database-sweep artifact.

    Evidence class: Cached sweep artifact (medium trust).
    """
    dimension = EnumDiagnosticDimension.DATABASE_PROJECTIONS
    evidence_source = "database_sweep_artifact"
    try:
        sweep_dir = ctx.omni_home / ".onex_state" / "database-sweep"
        if not sweep_dir.exists():
            # Try alternate directory name
            sweep_dir = ctx.omni_home / ".onex_state" / "db-sweep"

        if not sweep_dir.exists():
            return ModelDiagnosticDimensionResult(
                dimension=dimension,
                status=EnumReadinessStatus.WARN,
                check_count=0,
                valid_zero=False,
                actionable_items=[
                    "database sweep directory not found — run /onex:database_sweep"
                ],
                evidence_source=evidence_source,
            )

        result_files = sorted(
            sweep_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True
        )
        if not result_files:
            return ModelDiagnosticDimensionResult(
                dimension=dimension,
                status=EnumReadinessStatus.WARN,
                check_count=0,
                valid_zero=False,
                actionable_items=["no database sweep results found"],
                evidence_source=evidence_source,
            )

        latest_file = result_files[0]
        freshness = int(time.time() - latest_file.stat().st_mtime)
        threshold_seconds = ctx.freshness_threshold_hours * 3600

        if freshness > threshold_seconds:
            hours = freshness // 3600
            return ModelDiagnosticDimensionResult(
                dimension=dimension,
                status=EnumReadinessStatus.WARN,
                check_count=0,
                valid_zero=False,
                actionable_items=[
                    f"database sweep is {hours}h old — re-run /onex:database_sweep"
                ],
                evidence_source=evidence_source,
                freshness_seconds=freshness,
            )

        data: dict[str, Any] = json.loads(latest_file.read_text())
        unpopulated = data.get("unpopulated_tables", []) or data.get("empty_tables", [])
        total = data.get("total_tables", 0) or data.get("checked", 0)
        actionable = [f"unpopulated projection table: {t}" for t in unpopulated[:5]]
        if len(unpopulated) > 5:
            actionable.append(f"... and {len(unpopulated) - 5} more")

        status = (
            EnumReadinessStatus.PASS if not unpopulated else EnumReadinessStatus.WARN
        )

        return ModelDiagnosticDimensionResult(
            dimension=dimension,
            status=status,
            check_count=total,
            valid_zero=False,
            actionable_items=actionable,
            evidence_source=evidence_source,
            freshness_seconds=freshness,
            raw_detail=f"unpopulated={len(unpopulated)} total={total}",
        )
    except Exception as exc:
        return _wrap_exception(dimension, evidence_source, exc)


async def check_ci_status(
    ctx: DiagnosticsCheckContext,
) -> ModelDiagnosticDimensionResult:
    """GitHub API per-repo CI run status — 0 failing main-branch runs.

    Evidence class: GitHub API (medium trust — rate-limited).
    valid_zero=True: a repo with no CI runs is acceptable.
    Skipped in dry_run mode (returns WARN to signal no live check done).
    """
    dimension = EnumDiagnosticDimension.CI_STATUS
    evidence_source = "github_actions"

    if ctx.dry_run:
        return ModelDiagnosticDimensionResult(
            dimension=dimension,
            status=EnumReadinessStatus.WARN,
            check_count=0,
            valid_zero=True,
            actionable_items=["CI check skipped in dry_run mode"],
            evidence_source=evidence_source,
            raw_detail="dry_run=True",
        )

    try:
        headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
        if ctx.github_token:
            headers["Authorization"] = f"Bearer {ctx.github_token}"

        failing_repos: list[str] = []
        check_count = 0

        async with aiohttp.ClientSession(headers=headers) as session:
            for repo in ctx.github_repos:
                url = f"https://api.github.com/repos/{repo}/actions/runs?branch=main&per_page=5"
                try:
                    async with session.get(
                        url,
                        timeout=aiohttp.ClientTimeout(total=ctx.http_timeout),
                    ) as resp:
                        if resp.status == 404:
                            continue
                        if resp.status != 200:
                            failing_repos.append(f"{repo} (API error {resp.status})")
                            continue

                        data = await resp.json()
                        runs = data.get("workflow_runs", [])
                        check_count += len(runs)

                        failed = [
                            r["name"]
                            for r in runs
                            if r.get("conclusion")
                            in ("failure", "timed_out", "cancelled")
                        ]
                        if failed:
                            failing_repos.append(f"{repo}: {', '.join(failed[:3])}")
                except Exception:
                    pass

        actionable = [f"CI failing: {r}" for r in failing_repos]
        status = (
            EnumReadinessStatus.PASS if not failing_repos else EnumReadinessStatus.FAIL
        )

        return ModelDiagnosticDimensionResult(
            dimension=dimension,
            status=status,
            check_count=check_count,
            valid_zero=True,
            actionable_items=actionable,
            evidence_source=evidence_source,
            raw_detail=f"repos_checked={len(ctx.github_repos)} failing={len(failing_repos)}",
        )
    except Exception as exc:
        return _wrap_exception(dimension, evidence_source, exc)


async def check_coverage(
    ctx: DiagnosticsCheckContext,
) -> ModelDiagnosticDimensionResult:
    """Check test coverage from cached coverage sweep artifact.

    Evidence class: Cached sweep artifact (medium trust).
    """
    dimension = EnumDiagnosticDimension.COVERAGE
    evidence_source = "coverage_sweep_artifact"
    try:
        sweep_dir = ctx.omni_home / ".onex_state" / "coverage-sweep"
        if not sweep_dir.exists():
            return ModelDiagnosticDimensionResult(
                dimension=dimension,
                status=EnumReadinessStatus.WARN,
                check_count=0,
                valid_zero=False,
                actionable_items=[
                    "coverage sweep directory not found — run /onex:coverage_sweep"
                ],
                evidence_source=evidence_source,
            )

        result_files = sorted(
            sweep_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True
        )
        if not result_files:
            return ModelDiagnosticDimensionResult(
                dimension=dimension,
                status=EnumReadinessStatus.WARN,
                check_count=0,
                valid_zero=False,
                actionable_items=["no coverage sweep results found"],
                evidence_source=evidence_source,
            )

        latest_file = result_files[0]
        freshness = int(time.time() - latest_file.stat().st_mtime)
        threshold_seconds = ctx.freshness_threshold_hours * 3600

        if freshness > threshold_seconds:
            hours = freshness // 3600
            return ModelDiagnosticDimensionResult(
                dimension=dimension,
                status=EnumReadinessStatus.WARN,
                check_count=0,
                valid_zero=False,
                actionable_items=[
                    f"coverage sweep is {hours}h old — re-run /onex:coverage_sweep"
                ],
                evidence_source=evidence_source,
                freshness_seconds=freshness,
            )

        data: dict[str, Any] = json.loads(latest_file.read_text())
        below_threshold = data.get("below_threshold_repos", []) or data.get(
            "failing_repos", []
        )
        total = data.get("total_repos", 0) or data.get("checked", 0)
        actionable = [f"coverage below threshold: {r}" for r in below_threshold[:5]]
        if len(below_threshold) > 5:
            actionable.append(f"... and {len(below_threshold) - 5} more")

        status = (
            EnumReadinessStatus.PASS
            if not below_threshold
            else EnumReadinessStatus.WARN
        )

        return ModelDiagnosticDimensionResult(
            dimension=dimension,
            status=status,
            check_count=total,
            valid_zero=False,
            actionable_items=actionable,
            evidence_source=evidence_source,
            freshness_seconds=freshness,
            raw_detail=f"below_threshold={len(below_threshold)} total={total}",
        )
    except Exception as exc:
        return _wrap_exception(dimension, evidence_source, exc)


# Map from EnumDiagnosticDimension to the async check function
DIMENSION_CHECK_MAP = {
    EnumDiagnosticDimension.CONTRACT_HEALTH: check_contract_health,
    EnumDiagnosticDimension.GOLDEN_CHAIN: check_golden_chain,
    EnumDiagnosticDimension.RUNTIME_NODES: check_runtime_nodes,
    EnumDiagnosticDimension.HOOK_HEALTH: check_hook_health,
    EnumDiagnosticDimension.DATABASE_PROJECTIONS: check_database_projections,
    EnumDiagnosticDimension.CI_STATUS: check_ci_status,
    EnumDiagnosticDimension.COVERAGE: check_coverage,
}


async def run_dimension_checks(
    ctx: DiagnosticsCheckContext,
    dimensions: list[EnumDiagnosticDimension],
) -> list[ModelDiagnosticDimensionResult]:
    """Run selected dimension checks in parallel via asyncio.gather.

    return_exceptions=True ensures a failed check wraps the exception as a FAIL result
    rather than propagating and crashing the caller.
    """
    check_fns = [DIMENSION_CHECK_MAP[dim] for dim in dimensions]
    raw_results = await asyncio.gather(
        *[fn(ctx) for fn in check_fns],
        return_exceptions=True,
    )

    results: list[ModelDiagnosticDimensionResult] = []
    for i, result in enumerate(raw_results):
        if isinstance(result, BaseException):
            results.append(
                ModelDiagnosticDimensionResult(
                    dimension=dimensions[i],
                    status=EnumReadinessStatus.FAIL,
                    check_count=0,
                    valid_zero=False,
                    actionable_items=[
                        f"gather exception: {type(result).__name__}: {result}"
                    ],
                    evidence_source="unknown",
                    raw_detail=str(result),
                )
            )
        else:
            results.append(result)

    return results
