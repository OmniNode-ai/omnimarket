# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node runtime for the remote-lane runner's dispatch check.

``python -m omnimarket.nodes.node_wip_admission_compute.runtime_brief_gate --lane L [--repo R]
[--ticket T] [--pr X]...`` prints the refusal and exits 3, else exits 0. The runner is a script
that cannot import omnimarket, so it asks this through ``uv run``, the way it already reaches the
lab job submit.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from omnimarket.handlers.wip_admission_brief_gate import dispatch_wip_refusal

EXIT_ADMIT, EXIT_REFUSE = 0, 3


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ask the WIP admission for one dispatch"
    )
    parser.add_argument("--lane", required=True)
    parser.add_argument("--repo", default="")
    parser.add_argument("--ticket", default="")
    parser.add_argument("--pr", action="append", default=[])
    args = parser.parse_args(argv)
    refusal = dispatch_wip_refusal(
        lane=args.lane,
        repo=args.repo,
        ticket=args.ticket,
        prs=args.pr,
        environ=os.environ,
    )
    if refusal:
        sys.stdout.write(refusal + "\n")
        return EXIT_REFUSE
    return EXIT_ADMIT


if __name__ == "__main__":
    raise SystemExit(main())
