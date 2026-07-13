#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CI/pre-commit gate for node_contract_sweep (OMN-14542, class fix, parent
OMN-14531).

The census (`--repos`) is collected HERE, by the harness, via a real
filesystem probe — never typed by an operator and never documented as prose
in a skill file. This script:

1. Derives the repo name and OMNI_HOME from this script's own on-disk
   location (no operator input required for the default invocation).
2. Independently counts discoverable `contract.yaml` files under the repo
   root (the same exclusion rules as the node: skip `.venv`/`site-packages`,
   require `nodes` in the path) — a second, independent probe, so a
   narrowed/mis-scoped census inside the node itself cannot silently agree
   with itself.
3. Calls `NodeContractSweep.handle()` and enforces the fail-closed scope
   invariant: refuse to exit 0 unless `scanned_count > 0` AND
   `scanned_count == the independently-probed count`.
4. Reports real field/topic/node_type violations found in the corpus. The
   pre-existing backlog (5 major, 21 minor) was paid down under OMN-14544 —
   the 5 major violations were fixed at the source and the 21 minor
   `onex.snapshot.*` topics turned out to be a real, cross-repo-established
   projection-broadcast topic kind, not naming mistakes, so the separate
   `_SNAPSHOT_TOPIC_RE` accepts it. The corpus is now a real zero, so --strict is on
   by default in ci.yml and .pre-commit-config.yaml; pass --strict here too
   to block on any NEW violation.

Exit codes:
  0 — scope invariant holds (scanned_count > 0 and matches the independent
      probe); violations reported as a warning unless --strict.
  1 — scope invariant violated (ERROR status, scanned_count == 0, or a
      mismatch against the independent probe) — always blocking. Or,
      with --strict, real violations were found.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Add src/ to path so omnimarket imports work in CI without editable install
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from omnimarket.nodes.node_contract_sweep.handlers.handler_contract_sweep import (  # noqa: E402
    ContractSweepRequest,
    EnumSweepStatus,
    NodeContractSweep,
)


def _independent_contract_count(repo_root: Path) -> int:
    """Second, independent filesystem probe — deliberately NOT sharing code
    with the node's own scan loop, so a bug that narrows the node's own
    corpus cannot silently agree with the count used to validate it."""
    count = 0
    for contract_path in repo_root.rglob("contract.yaml"):
        if "nodes" not in str(contract_path):
            continue
        parts = contract_path.parts
        if ".venv" in parts or "site-packages" in parts:
            continue
        count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        default=str(_REPO_ROOT),
        help=(
            "Root of the repo to scan (default: this script's own repo — "
            "derived from disk location, not operator-typed)."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Also block (exit 1) on real field/topic/node_type violations.",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    repo_name = repo_root.name
    omni_home = repo_root.parent

    expected_count = _independent_contract_count(repo_root)

    os.environ["OMNI_HOME"] = str(omni_home)
    result = NodeContractSweep().handle(ContractSweepRequest(repos=[repo_name]))

    print(json.dumps(result.model_dump(mode="json"), indent=2))

    if result.status == EnumSweepStatus.ERROR:
        print(
            f"::error::contract_sweep scope ERROR — refusing to report PASS: "
            f"{result.scope_error}",
            file=sys.stderr,
        )
        return 1

    if result.scanned_count == 0:
        print(
            "::error::contract_sweep scanned zero contracts — refusing to "
            "report PASS over an empty scope.",
            file=sys.stderr,
        )
        return 1

    if result.scanned_count != expected_count:
        print(
            f"::error::contract_sweep scanned_count ({result.scanned_count}) "
            f"!= independently-probed contract count ({expected_count}) for "
            f"repo_root={repo_root} — the census narrowed or widened "
            "relative to what is actually on disk. Refusing to report PASS "
            "over an unverified scope.",
            file=sys.stderr,
        )
        return 1

    print(
        f"::notice::contract_sweep scope OK: scanned_count == "
        f"independently-probed count == {expected_count}"
    )

    if result.status == EnumSweepStatus.FAIL:
        print(
            f"::warning::contract_sweep found {len(result.violations)} "
            "pre-existing field/topic/node_type violation(s). Not yet "
            "blocking (tracked in OMN-14544); pass "
            "--strict to promote to blocking.",
            file=sys.stderr,
        )
        if args.strict:
            print(
                "::error::contract_sweep --strict: refusing to pass with "
                f"{len(result.violations)} violation(s).",
                file=sys.stderr,
            )
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
