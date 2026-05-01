# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local CLI entry point for node_platform_readiness."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
    DimensionInput,
    NodePlatformReadiness,
    PlatformReadinessRequest,
    PlatformReadinessResult,
    ReadinessStatus,
)

DRY_RUN_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="node_platform_readiness")
    parser.add_argument(
        "--output-format",
        choices=("json", "markdown"),
        default="json",
        help="Output format for the readiness report",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Shortcut for --output-format json",
    )
    parser.add_argument(
        "--dimension",
        action="append",
        default=[],
        help="Limit output to one dimension; may be passed more than once",
    )
    parser.add_argument(
        "--dimensions",
        default="",
        help="Comma-separated list of dimensions to include",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Use deterministic local inputs instead of probing host state",
    )
    return parser.parse_args(argv)


def _dry_run_dimensions(now: datetime) -> list[DimensionInput]:
    return [
        DimensionInput(
            name="plugin_version",
            critical=True,
            healthy=True,
            last_checked=now,
            details="dry-run plugin check passed",
        ),
        DimensionInput(
            name="docker_image_age",
            critical=True,
            healthy=True,
            last_checked=now,
            details="dry-run image age check passed",
        ),
        DimensionInput(
            name="migration_watermark",
            critical=True,
            healthy=True,
            last_checked=now,
            details="dry-run migration watermark check passed",
        ),
        DimensionInput(
            name="kafka_topic_coverage",
            critical=False,
            healthy=True,
            last_checked=now,
            details="dry-run topic coverage check passed",
        ),
        DimensionInput(
            name="pre_commit_installation",
            critical=False,
            healthy=True,
            last_checked=now,
            details="dry-run pre-commit check passed",
        ),
        DimensionInput(
            name="quality_score_coverage",
            critical=False,
            healthy=True,
            last_checked=now,
            details="dry-run quality score check passed",
        ),
        DimensionInput(
            name="baselines_freshness",
            critical=False,
            healthy=True,
            last_checked=now,
            details="dry-run baseline freshness check passed",
        ),
    ]


def _selected_dimensions(args: argparse.Namespace) -> set[str]:
    selected = {dimension.strip() for dimension in args.dimension if dimension.strip()}
    if args.dimensions:
        selected.update(
            dimension.strip()
            for dimension in args.dimensions.split(",")
            if dimension.strip()
        )
    return selected


def _filter_result(
    result: PlatformReadinessResult, selected: set[str]
) -> PlatformReadinessResult:
    if not selected:
        return result

    dimensions = [dim for dim in result.dimensions if dim.name in selected]
    found = {dim.name for dim in dimensions}
    missing = sorted(selected - found)
    if missing:
        raise ValueError(f"Unknown dimension(s): {', '.join(missing)}")

    blockers = [
        f"{dim.name}: {dim.details}"
        for dim in dimensions
        if dim.status == ReadinessStatus.FAIL
    ]
    degraded = [
        f"{dim.name}: {dim.details}"
        for dim in dimensions
        if dim.status == ReadinessStatus.WARN
    ]
    if blockers:
        overall = ReadinessStatus.FAIL
    elif degraded:
        overall = ReadinessStatus.WARN
    else:
        overall = ReadinessStatus.PASS

    return PlatformReadinessResult(
        overall=overall,
        dimensions=dimensions,
        blockers=blockers,
        degraded=degraded,
        timestamp=result.timestamp,
    )


def _render_markdown(result: PlatformReadinessResult) -> str:
    lines = [
        f"# Platform Readiness: {result.overall.value}",
        "",
        "| Dimension | Status | Freshness | Details |",
        "| --- | --- | --- | --- |",
    ]
    for dim in result.dimensions:
        lines.append(
            f"| {dim.name} | {dim.status.value} | {dim.freshness} | {dim.details} |"
        )
    if result.blockers:
        lines.extend(["", "## Blockers"])
        lines.extend(f"- {blocker}" for blocker in result.blockers)
    if result.degraded:
        lines.extend(["", "## Degraded"])
        lines.extend(f"- {item}" for item in result.degraded)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    now = DRY_RUN_NOW if args.dry_run else datetime.now(UTC)
    request = PlatformReadinessRequest(
        dimensions=_dry_run_dimensions(now) if args.dry_run else [],
        now=now,
    )

    raw_result = NodePlatformReadiness().handle(request)
    try:
        result = _filter_result(raw_result, _selected_dimensions(args))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)  # noqa: T201
        return 2

    output_format = "json" if args.json else args.output_format
    if output_format == "json":
        print(json.dumps(result.model_dump(mode="json"), indent=2))  # noqa: T201
    else:
        print(_render_markdown(result))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
