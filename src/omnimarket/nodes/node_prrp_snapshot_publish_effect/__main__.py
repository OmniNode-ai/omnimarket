# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""onex-prrp-snapshot-publish: the timer's command; prints one summary line, an ALARM on failure.

The target hosts are the rows of the declared lab host table (the ``lab_hosts`` overlay), read by the handler the
container builds (OMN-23396); no flag or file names a host, so the request carries none.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .container import container_prrp_snapshot_publish_effect
from .models import ModelPrrpSnapshotPublishRequest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="onex-prrp-snapshot-publish")
    parser.add_argument("--gate-script", type=Path, required=True)
    parser.add_argument("--repos-file", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--ssh-program", default="ssh")
    args = parser.parse_args(argv)
    result = container_prrp_snapshot_publish_effect.build_handler_prrp_snapshot_publish().handle(
        ModelPrrpSnapshotPublishRequest(
            gate_script=args.gate_script,
            repos_file=args.repos_file,
            hosts=(),
            state_dir=args.state_dir,
            home=args.home,
            destination=args.destination,
            python=Path(sys.executable),
            ssh_program=args.ssh_program,
        )
    )
    statuses = " ".join(f"{outcome.host}={outcome.status}" for outcome in result.hosts)
    if result.exit_code == 0:
        sys.stdout.write(
            f"prrp_variables_snapshot: OK captured_at={result.captured_at} "
            f"repos={result.repos} {statuses}\n"
        )
        return 0
    sys.stderr.write(
        f"ALARM prrp_variables_snapshot: FAILED, lab hosts keep their previous snapshot and the "
        f"gate refuses it after 24h: {statuses or 'no host reached'}\n"
    )
    for failure in result.failures:
        sys.stderr.write(f"ALARM prrp_variables_snapshot: {failure}\n")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
