#!/usr/bin/env python3
"""Capture the market-only skill baseline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from omnimarket.market_skill_baseline import (  # noqa: E402
    ModelMarketSkillBaselineReport,
    ModelMarketSkillResult,
    build_market_skill_baseline_report,
    capture_market_skill_baseline,
    iter_market_skill_baseline_results,
    render_markdown,
)


def _status(value: bool) -> str:
    return "pass" if value else "fail"


def _print_stream_result(item: ModelMarketSkillResult) -> None:
    sys.stdout.write(
        f"[market-skill] {item.skill_name} "
        f"node={item.contract.node_name} status={item.overall_status}\n"
    )
    sys.stdout.write(f"[market-skill] {item.skill_name} task={item.task_text}\n")
    sys.stdout.write(
        f"[market-skill] {item.skill_name} contract={item.contract.contract_name} "
        f"terminal_event={item.contract.terminal_event} "
        f"input_match={item.input_drift.matches}\n"
    )
    sys.stdout.write(
        f"[market-skill] {item.skill_name} cli={_status(item.cli_smoke.passed)} "
        f"rc={item.cli_smoke.returncode} command={' '.join(item.cli_smoke.command)} "
        f"summary={item.cli_smoke.summary}\n"
    )
    if item.cli_smoke.notes:
        sys.stdout.write(
            f"[market-skill] {item.skill_name} cli_notes={' | '.join(item.cli_smoke.notes)}\n"
        )
    if item.cli_smoke.stderr:
        sys.stdout.write(
            f"[market-skill] {item.skill_name} cli_stderr={item.cli_smoke.stderr}\n"
        )
    if item.pytest is not None:
        targets = item.pytest.summary.get("targets", [])
        sys.stdout.write(
            f"[market-skill] {item.skill_name} runtime_proof={_status(item.pytest.passed)} "
            f"rc={item.pytest.returncode} targets={targets}\n"
        )
        if item.pytest.notes:
            sys.stdout.write(
                f"[market-skill] {item.skill_name} runtime_output={' | '.join(item.pytest.notes)}\n"
            )
        if item.pytest.stderr:
            sys.stdout.write(
                f"[market-skill] {item.skill_name} runtime_stderr={item.pytest.stderr}\n"
            )
    sys.stdout.flush()


def _capture_streaming(
    *,
    run_pytest: bool,
    skill_names: set[str] | None,
) -> ModelMarketSkillBaselineReport:
    results: list[ModelMarketSkillResult] = []
    for item in iter_market_skill_baseline_results(
        run_pytest=run_pytest,
        skill_names=skill_names,
    ):
        _print_stream_result(item)
        results.append(item)
    return build_market_skill_baseline_report(results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skills",
        default="all",
        help="Comma-separated market skill names to include, or 'all'",
    )
    parser.add_argument(
        "--skip-pytest",
        action="store_true",
        help="Capture CLI smokes only",
    )
    parser.add_argument(
        "--json-out",
        help="Optional path to write the JSON baseline report",
    )
    parser.add_argument(
        "--markdown-out",
        help="Optional path to write the markdown baseline report",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any skill is not fully working",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="Print each market skill result as soon as its CLI smoke and runtime proof finish",
    )
    args = parser.parse_args(argv)

    skill_names = None
    if args.skills != "all":
        skill_names = {item.strip() for item in args.skills.split(",") if item.strip()}

    if args.stream:
        report = _capture_streaming(
            run_pytest=not args.skip_pytest,
            skill_names=skill_names,
        )
    else:
        report = capture_market_skill_baseline(
            run_pytest=not args.skip_pytest,
            skill_names=skill_names,
        )
    markdown = render_markdown(report)
    json_payload = report.model_dump(mode="json")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(json_payload, indent=2) + "\n", encoding="utf-8"
        )
    if args.markdown_out:
        Path(args.markdown_out).write_text(markdown + "\n", encoding="utf-8")

    sys.stdout.write(markdown + "\n")
    if args.strict and any(item.overall_status != "working" for item in report.skills):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
