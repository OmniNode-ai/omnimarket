# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the host drift check for one declared host; exit 1 when the host has drifted."""

import argparse
import sys
from pathlib import Path

from omnimarket.handlers.handler_host_profile import (
    parse_host_profile,
)
from omnimarket.handlers.handler_host_shell import (
    HandlerHostShell,
)
from omnimarket.models.model_host_profile import ModelHostDriftCheckRequest
from omnimarket.nodes.node_host_drift_check_effect.handlers.handler_host_drift_check import (
    HandlerHostDriftCheck,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument(
        "--local",
        action="store_true",
        help="measure and act on this host, not over ssh",
    )
    args = parser.parse_args(argv)
    profile = parse_host_profile(args.profiles.read_text(), args.host)
    if args.local:
        profile = profile.model_copy(update={"ssh_target": None})
    result = HandlerHostDriftCheck(HandlerHostShell()).handle(
        ModelHostDriftCheckRequest(profile=profile)
    )
    for finding in result.findings:
        sys.stdout.write(
            f"DRIFT {result.host} {finding.kind.value} {finding.subject}" + "\n"
        )
    sys.stdout.write(
        f"VERDICT {result.host}: {'IN_SYNC' if result.ok else 'DRIFTED'} findings={len(result.findings)}"
        + "\n"
    )
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
