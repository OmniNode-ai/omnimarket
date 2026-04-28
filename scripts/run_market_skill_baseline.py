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
    capture_market_skill_baseline,
    render_markdown,
)


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
    args = parser.parse_args(argv)

    skill_names = None
    if args.skills != "all":
        skill_names = {item.strip() for item in args.skills.split(",") if item.strip()}

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
