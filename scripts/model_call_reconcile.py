#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Print the model-server bypass report for one window (OMN-20299).

Read-only: it reads access-log files and a runs file, and writes nothing else.

    journalctl -u vllm-gpu0 --since '24 hours ago' -o short-iso --no-pager > vllm.log
    psql -Atc "select correlation_id, coalesce(caller_lane,'') from delegation_events
               where timestamp > now()-interval '24 hours'" -F $'\\t' > runs.tsv
    scripts/model_call_reconcile.py --log 201:8000=vllm.log --runs runs.tsv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from omnimarket.model_call_reconcile.reconcile import (
    build_report,
    classify,
    parse_access_log,
)


def _read_runs(path: Path) -> dict[str, str | None]:
    runs: dict[str, str | None] = {}
    for line in path.read_text().splitlines():
        cid, _, lane = line.partition("\t")
        if cid:
            runs[cid] = lane or None
    return runs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", action="append", required=True, metavar="SERVER=PATH")
    parser.add_argument("--runs", type=Path, required=True)
    args = parser.parse_args()

    requests = []
    for spec in args.log:
        server, _, path = spec.partition("=")
        requests.extend(parse_access_log(Path(path).read_text().splitlines(), server))
    report = build_report(classify(requests, _read_runs(args.runs)))

    out = sys.stdout
    out.write(f"requests={report.total} attributed={report.attributed} ")
    out.write(f"orphan_cid={report.orphan_correlation_id} bypass={report.bypass}\n")
    for lane, n in report.attributed_by_lane.items():
        out.write(f"attributed lane={lane} n={n}\n")
    for server, n in report.bypass_by_server.items():
        out.write(f"bypass server={server} n={n}\n")
    for sample in report.bypass_samples:
        out.write(
            f"sample {sample.timestamp} {sample.server} {sample.path} {sample.status}\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
