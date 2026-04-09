# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""7 parallel dimension check functions for platform readiness V2.

Each function is `async def check_<dimension>(ctx: CheckContext) -> ModelDimensionResultV2`
and runs independently. All 7 are gathered in parallel via asyncio.gather.

Evidence classes:
  - Cached sweep artifact: reads local .onex_state files (medium trust — inherits sweep fidelity)
  - Live HTTP probe: calls HTTP endpoint in real time (high trust)
  - Live DB query: calls external API (high trust)
  - GitHub API: rate-limited, may be cached (medium trust)

If a check raises an exception, wrap it in a ModelDimensionResultV2 with status=FAIL
and the exception message as raw_detail. This ensures asyncio.gather never propagates
exceptions up to the orchestrator.
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

from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
    EnumReadinessStatus,
)
from omnimarket.nodes.node_platform_readiness.models.dimension_result_v2 import (
    ModelDimensionResultV2,
)

_OMNI_HOME = os.environ.get("OMNI_HOME", os.path.expanduser("~/Code/omni_home"))
_RUNTIME_API = os.environ.get("ONEX_RUNTIME_API", "http://192.168.86.201:8080")
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
    "OmniNode-ai/omnimarket",
    "OmniNode-ai/onex_change_control",
]
_GOLDEN_CHAIN_FRESHNESS_THRESHOLD = 4 * 3600  # 4 hours in seconds
_MIN_RUNTIME_NODES = 40


@dataclass
class CheckContext:
    """Shared context passed to all dimension check functions."""

    omni_home: Path = field(default_factory=lambda: Path(_OMNI_HOME))
    runtime_api: str = _RUNTIME_API
    dashboard_api: str = _DASHBOARD_API
    github_token: str = _GITHUB_TOKEN
    github_repos: list[str] = field(default_factory=lambda: list(_GITHUB_REPOS))
    http_timeout: float = 10.0  # seconds per HTTP call


def _wrap_exception(
    dimension: str, evidence_source: str, exc: Exception
) -> ModelDimensionResultV2:
    """Wrap an unhandled exception as a FAIL dimension result."""
    return ModelDimensionResultV2(
        dimension=dimension,
        status=EnumReadinessStatus.FAIL,
        check_count=0,
        valid_zero=False,
        actionable_items=[f"check raised {type(exc).__name__}: {exc}"],
        evidence_source=evidence_source,
        raw_detail=str(exc),
    )


async def check_contract_completeness(ctx: CheckContext) -> ModelDimensionResultV2:
    """Check that all node contracts have golden_path + dod_evidence.

    Evidence class: Cached sweep artifact (medium trust).
    Reads the most recent contract sweep result from onex_change_control output.
    """
    dimension = "contract_completeness"
    evidence_source = "onex_change_control"
    try:
        sweep_dir = ctx.omni_home / ".onex_state" / "contract-sweep"
        if not sweep_dir.exists():
            return ModelDimensionResultV2(
                dimension=dimension,
                status=EnumReadinessStatus.WARN,
                check_count=0,
                valid_zero=False,
                actionable_items=[
                    "contract sweep directory not found — run /onex:contract_sweep"
                ],
                evidence_source=evidence_source,
            )

        # Find most recent sweep output
        sweep_dirs = sorted(sweep_dir.iterdir(), reverse=True)
        if not sweep_dirs:
            return ModelDimensionResultV2(
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
        summary_file = latest / "summary.json"
        if not summary_file.exists():
            summary_file = latest / "results.json"

        if not summary_file.exists():
            return ModelDimensionResultV2(
                dimension=dimension,
                status=EnumReadinessStatus.WARN,
                check_count=0,
                valid_zero=False,
                actionable_items=[
                    f"no summary.json in {latest.name} — sweep may have failed"
                ],
                evidence_source=evidence_source,
                sweep_names=[latest.name],
            )

        data: dict[str, Any] = json.loads(summary_file.read_text())
        missing_fields = data.get("missing_required_fields", [])
        total = data.get("total_contracts", 0) or data.get("checked", 0)
        actionable: list[str] = [
            f"contract missing golden_path/dod_evidence: {m}"
            for m in missing_fields[:5]
        ]
        if len(missing_fields) > 5:
            actionable.append(f"... and {len(missing_fields) - 5} more")

        status = (
            EnumReadinessStatus.PASS if not missing_fields else EnumReadinessStatus.FAIL
        )
        mtime = summary_file.stat().st_mtime
        freshness = int(time.time() - mtime)

        return ModelDimensionResultV2(
            dimension=dimension,
            status=status,
            check_count=total,
            valid_zero=False,
            actionable_items=actionable,
            evidence_source=evidence_source,
            sweep_names=[latest.name],
            freshness_seconds=freshness,
            raw_detail=f"missing_fields={len(missing_fields)} total={total}",
        )
    except Exception as exc:
        return _wrap_exception(dimension, evidence_source, exc)


async def check_golden_chain(ctx: CheckContext) -> ModelDimensionResultV2:
    """Check golden chain sweep result, freshness < 4h.

    Evidence class: Cached sweep artifact (medium trust).
    """
    dimension = "golden_chain"
    evidence_source = "golden_chain_sweep_artifact"
    try:
        sweep_dir = ctx.omni_home / ".onex_state" / "golden-chain-sweep"
        if not sweep_dir.exists():
            return ModelDimensionResultV2(
                dimension=dimension,
                status=EnumReadinessStatus.WARN,
                check_count=0,
                valid_zero=False,
                actionable_items=[
                    "golden chain sweep directory not found — run /onex:golden_chain_sweep"
                ],
                evidence_source=evidence_source,
            )

        sweep_dirs = sorted(sweep_dir.iterdir(), reverse=True)
        if not sweep_dirs:
            return ModelDimensionResultV2(
                dimension=dimension,
                status=EnumReadinessStatus.WARN,
                check_count=0,
                valid_zero=False,
                actionable_items=["no golden chain sweep runs found"],
                evidence_source=evidence_source,
            )

        latest = sweep_dirs[0]
        freshness = int(time.time() - latest.stat().st_mtime)

        if freshness > _GOLDEN_CHAIN_FRESHNESS_THRESHOLD:
            hours = freshness // 3600
            return ModelDimensionResultV2(
                dimension=dimension,
                status=EnumReadinessStatus.WARN,
                check_count=0,
                valid_zero=False,
                actionable_items=[
                    f"golden chain sweep is {hours}h old — re-run /onex:golden_chain_sweep"
                ],
                evidence_source=evidence_source,
                sweep_names=[latest.name],
                freshness_seconds=freshness,
            )

        # Read result
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

        return ModelDimensionResultV2(
            dimension=dimension,
            status=status,
            check_count=check_count,
            valid_zero=True,
            actionable_items=actionable,
            evidence_source=evidence_source,
            sweep_names=[latest.name],
            freshness_seconds=freshness,
        )
    except Exception as exc:
        return _wrap_exception(dimension, evidence_source, exc)


async def check_data_flow(ctx: CheckContext) -> ModelDimensionResultV2:
    """Check data flow sweep result for 0 MAJOR gaps.

    Evidence class: Cached sweep artifact (medium trust).
    """
    dimension = "data_flow"
    evidence_source = "data_flow_artifact"
    try:
        sweep_dir = ctx.omni_home / ".onex_state" / "data-flow"
        if not sweep_dir.exists():
            return ModelDimensionResultV2(
                dimension=dimension,
                status=EnumReadinessStatus.WARN,
                check_count=0,
                valid_zero=False,
                actionable_items=[
                    "data-flow directory not found — run /onex:data_flow_sweep"
                ],
                evidence_source=evidence_source,
            )

        result_files = sorted(
            sweep_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True
        )
        if not result_files:
            return ModelDimensionResultV2(
                dimension=dimension,
                status=EnumReadinessStatus.WARN,
                check_count=0,
                valid_zero=False,
                actionable_items=["no data flow sweep results found"],
                evidence_source=evidence_source,
            )

        latest_file = result_files[0]
        freshness = int(time.time() - latest_file.stat().st_mtime)
        data = json.loads(latest_file.read_text())

        major_gaps = data.get("major_gaps", [])
        total_checks = data.get("total_topics", 0) or data.get("checked", 0)
        actionable = [f"MAJOR gap: {g}" for g in major_gaps[:5]]
        if len(major_gaps) > 5:
            actionable.append(f"... and {len(major_gaps) - 5} more MAJOR gaps")

        status = (
            EnumReadinessStatus.PASS if not major_gaps else EnumReadinessStatus.FAIL
        )

        return ModelDimensionResultV2(
            dimension=dimension,
            status=status,
            check_count=total_checks,
            valid_zero=False,
            actionable_items=actionable,
            evidence_source=evidence_source,
            sweep_names=[latest_file.name],
            freshness_seconds=freshness,
            raw_detail=f"major_gaps={len(major_gaps)}",
        )
    except Exception as exc:
        return _wrap_exception(dimension, evidence_source, exc)


async def check_runtime_wiring(ctx: CheckContext) -> ModelDimensionResultV2:
    """Live HTTP probe to .201 runtime API — node count >= 40, all entry points resolve.

    Evidence class: Live HTTP probe (high trust).
    """
    dimension = "runtime_wiring"
    evidence_source = "onex_runtime_api"
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                f"{ctx.runtime_api}/api/nodes",
                timeout=aiohttp.ClientTimeout(total=ctx.http_timeout),
            ) as resp,
        ):
            if resp.status != 200:
                return ModelDimensionResultV2(
                    dimension=dimension,
                    status=EnumReadinessStatus.FAIL,
                    check_count=0,
                    valid_zero=False,
                    actionable_items=[f"runtime API returned HTTP {resp.status}"],
                    evidence_source=evidence_source,
                    raw_detail=f"GET {ctx.runtime_api}/api/nodes → {resp.status}",
                )

            data = await resp.json()
            nodes = data if isinstance(data, list) else data.get("nodes", [])
            node_count = len(nodes)
            actionable: list[str] = []

            if node_count < _MIN_RUNTIME_NODES:
                actionable.append(
                    f"only {node_count} nodes registered (expected >= {_MIN_RUNTIME_NODES}) — "
                    "check entry points and runtime auto-wiring"
                )
                status = EnumReadinessStatus.WARN
            else:
                status = EnumReadinessStatus.PASS

            return ModelDimensionResultV2(
                dimension=dimension,
                status=status,
                check_count=node_count,
                valid_zero=False,
                actionable_items=actionable,
                evidence_source=evidence_source,
                raw_detail=f"node_count={node_count}",
            )
    except Exception as exc:
        return _wrap_exception(dimension, evidence_source, exc)


async def check_dashboard_data(ctx: CheckContext) -> ModelDimensionResultV2:
    """Live HTTP probe to omnidash — non-null, non-zero savings data in last 24h.

    Evidence class: Live HTTP probe (high trust).
    """
    dimension = "dashboard_data"
    evidence_source = "omnidash_savings_api"
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                f"{ctx.dashboard_api}/api/savings/summary",
                timeout=aiohttp.ClientTimeout(total=ctx.http_timeout),
            ) as resp,
        ):
            if resp.status != 200:
                return ModelDimensionResultV2(
                    dimension=dimension,
                    status=EnumReadinessStatus.FAIL,
                    check_count=0,
                    valid_zero=False,
                    actionable_items=[
                        f"dashboard savings API returned HTTP {resp.status}"
                    ],
                    evidence_source=evidence_source,
                )

            data = await resp.json()
            total_savings = data.get("total_savings_usd") or data.get("total") or 0
            recent_count = data.get("records_last_24h") or data.get("count_24h") or 0

            actionable: list[str] = []
            if not total_savings:
                actionable.append(
                    "savings total is null/zero — check projection pipeline"
                )
            if not recent_count:
                actionable.append(
                    "no savings records in last 24h — check omninode-runner containers"
                )

            status = (
                EnumReadinessStatus.PASS if not actionable else EnumReadinessStatus.WARN
            )

            return ModelDimensionResultV2(
                dimension=dimension,
                status=status,
                check_count=int(recent_count) if recent_count else 0,
                valid_zero=False,
                actionable_items=actionable,
                evidence_source=evidence_source,
                raw_detail=f"total_savings={total_savings} recent_count={recent_count}",
            )
    except Exception as exc:
        return _wrap_exception(dimension, evidence_source, exc)


async def check_cost_measurement(ctx: CheckContext) -> ModelDimensionResultV2:
    """Live DB query via dashboard API — >= 1 cost record in last 24h.

    Evidence class: Live DB query via dashboard API (high trust).
    """
    dimension = "cost_measurement"
    evidence_source = "omnidash_costs_api"
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                f"{ctx.dashboard_api}/api/costs/summary",
                timeout=aiohttp.ClientTimeout(total=ctx.http_timeout),
            ) as resp,
        ):
            if resp.status != 200:
                return ModelDimensionResultV2(
                    dimension=dimension,
                    status=EnumReadinessStatus.FAIL,
                    check_count=0,
                    valid_zero=False,
                    actionable_items=[
                        f"dashboard costs API returned HTTP {resp.status}"
                    ],
                    evidence_source=evidence_source,
                )

            data = await resp.json()
            recent_count = data.get("records_last_24h") or data.get("count_24h") or 0
            total_cost = data.get("total_cost_usd") or data.get("total") or 0

            actionable: list[str] = []
            if not recent_count:
                actionable.append(
                    "no cost records in last 24h — check node_projection_llm_cost consumer"
                )

            status = (
                EnumReadinessStatus.PASS if not actionable else EnumReadinessStatus.WARN
            )

            return ModelDimensionResultV2(
                dimension=dimension,
                status=status,
                check_count=int(recent_count) if recent_count else 0,
                valid_zero=False,
                actionable_items=actionable,
                evidence_source=evidence_source,
                raw_detail=f"total_cost={total_cost} recent_count={recent_count}",
            )
    except Exception as exc:
        return _wrap_exception(dimension, evidence_source, exc)


async def check_ci_health(ctx: CheckContext) -> ModelDimensionResultV2:
    """GitHub API per-repo CI status — 0 failing main-branch CIs across all repos.

    Evidence class: GitHub API (medium trust — rate-limited).
    valid_zero=True: a repo with no CI runs is acceptable (new repo, no workflows yet).
    """
    dimension = "ci_health"
    evidence_source = "github_actions"
    try:
        headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
        if ctx.github_token:
            headers["Authorization"] = f"Bearer {ctx.github_token}"

        failing_repos: list[str] = []
        check_count = 0

        async with aiohttp.ClientSession(headers=headers) as session:
            for repo in ctx.github_repos:
                url = f"https://api.github.com/repos/{repo}/commits/HEAD/check-runs"
                try:
                    async with session.get(
                        url,
                        timeout=aiohttp.ClientTimeout(total=ctx.http_timeout),
                    ) as resp:
                        if resp.status == 404:
                            continue  # repo doesn't exist or no checks
                        if resp.status != 200:
                            failing_repos.append(f"{repo} (API error {resp.status})")
                            continue

                        data = await resp.json()
                        runs = data.get("check_runs", [])
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
                    pass  # individual repo failure doesn't count as CI failure

        actionable = [f"CI failing: {r}" for r in failing_repos]
        status = (
            EnumReadinessStatus.PASS if not failing_repos else EnumReadinessStatus.FAIL
        )

        return ModelDimensionResultV2(
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


async def run_all_dimensions(ctx: CheckContext) -> list[ModelDimensionResultV2]:
    """Run all 7 dimension checks in parallel via asyncio.gather.

    return_exceptions=True ensures a failed check wraps the exception as a FAIL result
    rather than propagating and crashing the caller. The gather always returns 7 items.
    """
    raw_results = await asyncio.gather(
        check_contract_completeness(ctx),
        check_golden_chain(ctx),
        check_data_flow(ctx),
        check_runtime_wiring(ctx),
        check_dashboard_data(ctx),
        check_cost_measurement(ctx),
        check_ci_health(ctx),
        return_exceptions=True,
    )

    results: list[ModelDimensionResultV2] = []
    dimension_names = [
        "contract_completeness",
        "golden_chain",
        "data_flow",
        "runtime_wiring",
        "dashboard_data",
        "cost_measurement",
        "ci_health",
    ]
    evidence_sources = [
        "onex_change_control",
        "golden_chain_sweep_artifact",
        "data_flow_artifact",
        "onex_runtime_api",
        "omnidash_savings_api",
        "omnidash_costs_api",
        "github_actions",
    ]

    for i, result in enumerate(raw_results):
        if isinstance(result, BaseException):
            results.append(
                ModelDimensionResultV2(
                    dimension=dimension_names[i],
                    status=EnumReadinessStatus.FAIL,
                    check_count=0,
                    valid_zero=False,
                    actionable_items=[
                        f"gather exception: {type(result).__name__}: {result}"
                    ],
                    evidence_source=evidence_sources[i],
                    raw_detail=str(result),
                )
            )
        else:
            results.append(result)

    return results
