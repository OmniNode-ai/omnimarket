# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_contract_sweep.

Usage:
    python -m omnimarket.nodes.node_contract_sweep --repos REPO,... [--dry-run]

``--repos`` is REQUIRED (OMN-14542). It must be a real, harness-collected
census — e.g. a filesystem probe the caller (CI workflow step, pre-commit
hook script) runs itself. It is never an operator-typed convenience default;
the previous "empty = scan everything under OMNI_HOME" fallback is exactly
what produced a false-clean receipt (9 contracts checked while 941 existed).

Outputs JSON to stdout: ContractSweepResult model.

Exit codes:
    0 — status == PASS (scope resolved, zero violations)
    1 — status == FAIL (scope resolved, violations found) or
        status == ERROR (scope could not be trusted: missing OMNI_HOME, a
        requested repo absent on disk, or scanned_count == 0). ERROR is
        never reported as a clean exit — "scanned nothing" and "all healthy"
        must never be arithmetically identical.
"""

from __future__ import annotations

import argparse
import logging
import sys

from omnimarket.nodes.node_contract_sweep.handlers.handler_contract_sweep import (
    ContractSweepRequest,
    EnumSweepStatus,
    NodeContractSweep,
)

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Contract compliance sweep.")
    parser.add_argument(
        "--repos",
        required=True,
        help=(
            "Comma-separated repo names to scan. REQUIRED — must be a "
            "harness-collected census (a real filesystem probe), never an "
            "operator-typed default."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Report violations without creating tickets",
    )
    args = parser.parse_args()

    repos = [r.strip() for r in args.repos.split(",") if r.strip()]
    if not repos:
        _log.error(
            "Refusing to report PASS over an empty --repos census "
            "(argument was present but resolved to zero entries)."
        )
        sys.exit(1)

    request = ContractSweepRequest(repos=repos, dry_run=args.dry_run)

    handler = NodeContractSweep()
    result = handler.handle(request)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.status != EnumSweepStatus.PASS:
        if result.status == EnumSweepStatus.ERROR:
            _log.error(
                "contract_sweep scope ERROR — refusing to report PASS: %s",
                result.scope_error,
            )
        else:
            _log.error(
                "contract_sweep FAIL — %d violation(s) across %d contract(s) checked.",
                len(result.violations),
                result.scanned_count,
            )
        sys.exit(1)


if __name__ == "__main__":
    main()
