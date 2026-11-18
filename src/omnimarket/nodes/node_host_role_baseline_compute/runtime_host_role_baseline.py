# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Derive one host's expected set from its declared role and, with --check, hold the host to it (OMN-24263).

The host table row names the role (overlay store, ``ONEX_SKILL_OVERLAY_ROOTS``), the node's overlay names the baselines. The
derived ModelHostProfile is what node_host_drift_check_effect compares the measured host to. Exit 0 in sync or derived,
1 drifted, 2 refused (no role, no baseline, no such host row).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from pathlib import Path

from omnimarket.handlers.handler_host_shell import HandlerHostShell
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.models.model_host_profile import ModelHostDriftCheckRequest
from omnimarket.nodes.node_host_drift_check_effect.handlers.handler_host_drift_check import (
    HandlerHostDriftCheck,
)
from omnimarket.nodes.node_host_role_baseline_compute.handlers.handler_host_role_baseline import (
    HandlerHostRoleBaseline,
)
from omnimarket.nodes.node_host_role_baseline_compute.models import (
    ModelHostRoleBaselineRequest,
)
from omnimarket.nodes.node_host_role_baseline_compute.overlay import load_baselines

EXIT_DRIFTED = 1
EXIT_REFUSED = 2


def main(
    argv: list[str] | None = None, environ: Mapping[str, str] | None = None
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="the host table row name")
    parser.add_argument("--omni-home", dest="registry_root", type=Path, required=True)
    parser.add_argument(
        "--check",
        action="store_true",
        help="measure this host and compare it to the derived expected set",
    )
    args = parser.parse_args(argv)
    rows = {row.name: row for row in resolve_lab_hosts_table(environ).hosts}
    row = rows.get(args.host)
    if row is None:
        sys.stdout.write(
            f"REFUSED {args.host}: no row in the host table; declared: {sorted(rows)}\n"
        )
        return EXIT_REFUSED
    answer = HandlerHostRoleBaseline().handle(
        ModelHostRoleBaselineRequest(
            host=args.host,
            host_role=row.host_role,
            registry_root=str(args.registry_root),
            ssh_target=None,
            baselines=load_baselines(environ),
        )
    )
    if answer.profile is None:
        sys.stdout.write(f"REFUSED {args.host}: {answer.refusal} {answer.detail}\n")
        return EXIT_REFUSED
    profile = answer.profile
    sys.stdout.write(
        f"ROLE {args.host} role={answer.host_role} units={len(profile.systemd_units)} clones={len(profile.clones)}\n"
    )
    if not args.check:
        return 0
    result = HandlerHostDriftCheck(HandlerHostShell()).handle(
        ModelHostDriftCheckRequest(profile=profile)
    )
    for finding in result.findings:
        sys.stdout.write(
            f"DRIFT {result.host} {finding.kind.value} {finding.subject}\n"
        )
    sys.stdout.write(
        f"VERDICT {result.host}: {'IN_SYNC' if result.ok else 'DRIFTED'} findings={len(result.findings)}\n"
    )
    return 0 if result.ok else EXIT_DRIFTED


if __name__ == "__main__":
    sys.exit(main())
