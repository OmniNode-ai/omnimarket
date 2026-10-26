# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Summarize TLC model checker results: one line per results/*.out file."""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from omnimarket.nodes.node_tlc_result_summary_effect.models.model_tlc_result_summary import (
    ModelTlcResultSummaryRequest,
    ModelTlcResultSummaryResult,
)


def summarize_run_directory(run_dir: Path) -> list[str]:
    """Build one summary line per results/*.out file in *run_dir*."""
    rows: list[str] = []
    for f in sorted(glob.glob(str(run_dir / "results" / "*.out"))):
        n = os.path.basename(f)[:-4]
        with open(f) as fh:
            t = fh.read()
        cfg = ""
        cfg_path = run_dir / f"{n}.cfg"
        if os.path.exists(cfg_path):
            with open(cfg_path) as fh:
                cfg = fh.read()
        chk = " ".join(re.findall(r"^(?:INVARIANTS|PROPERTIES) (.*)$", cfg, re.M))
        m = re.search(
            r"(Invariant \w+ is violated|Temporal property \w+ was violated|No error has been found)",
            t,
        )
        v = (
            m.group(1)
            if m
            else ("running or stopped" if "rc=" not in t else "no verdict")
        )
        ds = re.findall(r"([\d,]+) distinct states found", t)
        d = re.findall(r"depth of the complete state graph search is (\d+)", t)
        tr = len(re.findall(r"^State \d+:", t, re.M))
        depth = d[-1] if d else "-"
        trace = tr or "-"
        rows.append(
            f"{n} | {chk} | {v} | {ds[-1] if ds else '-'} | depth {depth} | trace {trace}"
        )
    return rows


class HandlerTlcResultSummary:
    """Handler that summarizes TLC model checker results."""

    def handle(
        self, request: ModelTlcResultSummaryRequest
    ) -> ModelTlcResultSummaryResult:
        return ModelTlcResultSummaryResult(
            rows=tuple(summarize_run_directory(request.run_dir))
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m omnimarket.nodes.node_tlc_result_summary_effect"
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=Path.cwd(),
        help="Run directory containing results/*.out files.",
    )
    args = parser.parse_args(argv)
    rows = summarize_run_directory(args.run_dir)
    sys.stdout.write("\n".join(rows) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
