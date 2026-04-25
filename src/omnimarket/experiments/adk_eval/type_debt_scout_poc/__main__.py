# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI runner for the Track B POC.

Loads the P5 input findings, dispatches the handler against Qwen3-Coder,
captures latency across repeated runs, and writes evidence artifacts.

Usage:
    uv run python -m omnimarket.experiments.adk_eval.type_debt_scout_poc \
        --input src/omnimarket/experiments/adk_eval/eval/input_findings.jsonl \
        --output <path>/track_b_output.json \
        --metrics <path>/track_b_metrics.json \
        --runs 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.experiments.adk_eval.tools.mypy_parser import ModelMypyFinding
from omnimarket.experiments.adk_eval.type_debt_scout_poc.handler_type_debt_scout import (
    ModelTrackBConfig,
    _build_router,
    resolve_base_url_from_env,
    run_type_debt_scout,
)


def _load_findings(path: Path) -> list[ModelMypyFinding]:
    raw = path.read_text(encoding="utf-8")
    findings: list[ModelMypyFinding] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        payload = json.loads(line)
        column_raw = payload.get("column")
        if column_raw is not None and isinstance(column_raw, int) and column_raw < 0:
            payload["column"] = None
        normalized = {
            "file": payload["file"],
            "line": payload["line"],
            "column": payload.get("column"),
            "severity": payload["severity"],
            "error_code": payload.get("error_code") or payload["code"],
            "message": payload["message"],
        }
        findings.append(ModelMypyFinding.model_validate(normalized))
    return findings


async def _measure_runs(
    findings: list[ModelMypyFinding],
    *,
    config: ModelTrackBConfig,
    runs: int,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Run the handler ``runs`` times on a single shared router.

    Returns (per_run_records, aggregate_metrics). The first successful
    report is kept as the canonical output.
    """
    router = await _build_router(config)
    try:
        records: list[dict[str, object]] = []
        canonical_report: dict[str, object] | None = None
        for idx in range(1, runs + 1):
            started = time.monotonic()
            try:
                report = await run_type_debt_scout(
                    findings, config=config, router=router
                )
            except Exception as exc:
                elapsed = time.monotonic() - started
                records.append(
                    {
                        "run": idx,
                        "ok": False,
                        "elapsed_seconds": elapsed,
                        "error": repr(exc),
                    }
                )
                continue
            elapsed = time.monotonic() - started
            if canonical_report is None:
                canonical_report = json.loads(report.model_dump_json())
            records.append(
                {
                    "run": idx,
                    "ok": True,
                    "elapsed_seconds": elapsed,
                    "handler_latency_seconds": report.latency_seconds,
                    "priorities_count": len(report.findings_prioritized),
                }
            )
    finally:
        for provider_name in list(router._providers.keys()):  # noqa: SLF001
            await router._providers[provider_name].close()  # noqa: SLF001

    successful = [r for r in records if r.get("ok")]
    latencies = [float(r["elapsed_seconds"]) for r in successful]  # type: ignore[arg-type]
    aggregate: dict[str, object] = {
        "runs_total": len(records),
        "runs_ok": len(successful),
        "runs_failed": len(records) - len(successful),
        "latency_median_seconds": statistics.median(latencies) if latencies else None,
        "latency_mean_seconds": statistics.fmean(latencies) if latencies else None,
        "latency_min_seconds": min(latencies) if latencies else None,
        "latency_max_seconds": max(latencies) if latencies else None,
        "per_run": records,
    }
    return (
        [canonical_report] if canonical_report is not None else [],
        aggregate,
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="type_debt_scout_poc",
        description="Track B POC runner for ADK evaluation",
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--repo-name", default="omnibase_core")
    parser.add_argument("--base-url", default=None)
    parser.add_argument(
        "--model-id",
        default="cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit",
    )
    parser.add_argument("--max-tokens", type=int, default=4096)
    return parser.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> int:
    findings = _load_findings(args.input)
    if not findings:
        print("No findings loaded from input; aborting.", file=sys.stderr)  # noqa: T201
        return 2

    base_url = args.base_url or resolve_base_url_from_env()
    config = ModelTrackBConfig(
        repo_name=args.repo_name,
        base_url=base_url,
        model_id=args.model_id,
        max_tokens=args.max_tokens,
    )

    started = datetime.now(UTC)
    canonical_list, aggregate = await _measure_runs(
        findings, config=config, runs=args.runs
    )
    ended = datetime.now(UTC)

    if not canonical_list:
        print("All runs failed; nothing to write.", file=sys.stderr)  # noqa: T201
        return 3

    canonical = canonical_list[0]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(canonical, indent=2), encoding="utf-8")

    metrics = {
        "track": "B",
        "runner": "omnimarket.experiments.adk_eval.type_debt_scout_poc",
        "base_url": config.base_url,
        "model_id": config.model_id,
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "input_path": str(args.input),
        "input_findings_count": len(findings),
        **aggregate,
    }
    args.metrics.parent.mkdir(parents=True, exist_ok=True)
    args.metrics.write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    summary = {
        "output": str(args.output),
        "metrics": str(args.metrics),
        "runs_ok": aggregate["runs_ok"],
        "runs_total": aggregate["runs_total"],
        "latency_median_seconds": aggregate["latency_median_seconds"],
    }
    print(json.dumps(summary, indent=2))  # noqa: T201
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:]) if argv is None else argv)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
