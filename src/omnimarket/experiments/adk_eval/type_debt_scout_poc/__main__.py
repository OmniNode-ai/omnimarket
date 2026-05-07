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

from omnimarket.experiments.adk_eval.tools.mypy_parser import (
    ModelMypyFinding,
    parse_mypy_jsonl,
)
from omnimarket.experiments.adk_eval.type_debt_scout_poc.handler_type_debt_scout import (
    ModelTrackBConfig,
    _build_router,
    resolve_base_url_from_env,
    run_type_debt_scout,
)


def _load_findings(path: Path) -> list[ModelMypyFinding]:
    """Load mypy JSONL findings via the shared parser.

    Delegates column normalization and error_code derivation to
    ``parse_mypy_jsonl`` so the CLI runner cannot drift from the canonical
    parsing rules used elsewhere in the toolchain.
    """
    return parse_mypy_jsonl(path.read_text(encoding="utf-8"))


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
    close_errors: list[str] = []
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
        # Per-provider try/except so a single broken close() does not
        # discard the run records and aggregate metrics we just collected.
        for provider_name in list(router._providers.keys()):  # noqa: SLF001
            try:
                await router._providers[provider_name].close()  # noqa: SLF001
            except Exception as exc:
                close_errors.append(f"{provider_name}: {exc!r}")

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
    if close_errors:
        aggregate["provider_close_errors"] = close_errors
    return (
        [canonical_report] if canonical_report is not None else [],
        aggregate,
    )


def _positive_int(value: str) -> int:
    """argparse type that rejects 0 and negative integers.

    --runs 0 used to fall through and surface as "All runs failed", which
    is a confusing CLI for a boundary-check bug rather than a real failure.
    """
    parsed = int(value)
    if parsed < 1:
        msg = f"must be >= 1 (got {parsed})"
        raise argparse.ArgumentTypeError(msg)
    return parsed


# ModelTrackBConfig.max_tokens is constrained to ge=256; mirror that bound at
# the argparse boundary so a CLI input error fails fast with a clear message
# instead of raising as a Pydantic ValidationError deeper in the pipeline.
_MIN_MAX_TOKENS = 256


def _max_tokens_int(value: str) -> int:
    """argparse type for --max-tokens; mirrors the model-side ge=256 bound."""
    parsed = int(value)
    if parsed < _MIN_MAX_TOKENS:
        msg = f"must be >= {_MIN_MAX_TOKENS} (got {parsed})"
        raise argparse.ArgumentTypeError(msg)
    return parsed


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="type_debt_scout_poc",
        description="Track B POC runner for ADK evaluation",
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--runs", type=_positive_int, default=5)
    parser.add_argument("--repo-name", default="omnibase_core")
    parser.add_argument("--base-url", default=None)
    parser.add_argument(
        "--model-id",
        default="cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit",  # onex-allow-model-id OMN-10580 reason="experiment CLI dev default; override via --model-id"
    )
    parser.add_argument("--max-tokens", type=_max_tokens_int, default=4096)
    return parser.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> int:
    try:
        findings = _load_findings(args.input)
    except (OSError, ValueError) as exc:
        # OSError: file missing / unreadable. ValueError: malformed JSONL
        # surfaced by parse_mypy_jsonl. Convert to a clean CLI error so
        # operators get an actionable message instead of a raw traceback.
        print(  # noqa: T201
            f"Failed to load input findings from {args.input}: {exc}",
            file=sys.stderr,
        )
        return 2

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

    # Always persist metrics — even when every run failed, the per_run
    # records, latency summary, and counts are the failure evidence we need
    # most. Returning early without writing them loses exactly the data
    # that would explain the failure post-mortem.
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

    if not canonical_list:
        print(  # noqa: T201
            f"All {args.runs} run(s) failed; metrics written to {args.metrics}.",
            file=sys.stderr,
        )
        return 3

    canonical = canonical_list[0]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(canonical, indent=2), encoding="utf-8")

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
