# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_linear_snapshot_effect``: write today's Linear snapshot JSON."""

from __future__ import annotations

import argparse
import sys

from .handlers.handler_linear_snapshot import HandlerLinearSnapshot
from .models.model_linear_snapshot import ModelLinearSnapshotRequest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="linear_snapshot",
        description="Write linear-<UTC date>.json: the signed-in subject's open Linear issues.",
    )
    ap.add_argument("--out-dir", required=True, help="the directory to write into")
    ap.add_argument("--date", help="the UTC date in the file name (default: today)")
    args = ap.parse_args(argv)
    result = HandlerLinearSnapshot().handle(
        ModelLinearSnapshotRequest(out_dir=args.out_dir, date=args.date)
    )
    line = (
        f"linear-snapshot | {result.status} path={result.path} "
        f"issues={result.issue_count} with_description={result.with_description}"
        + (f" mode={result.mode}" if result.mode else "")
        + (f" age_s={result.age_s}" if result.age_s is not None else "")
    )
    sys.stdout.write(
        line + (f" reason: {result.reason}" if result.reason else "") + "\n"
    )
    return 1 if result.status == "failed" else 0
