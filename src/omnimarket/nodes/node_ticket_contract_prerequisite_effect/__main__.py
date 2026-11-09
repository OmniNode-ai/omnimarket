# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local pre-push and CI adapter for the contract-backed prerequisite node."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from omnimarket.nodes.node_ticket_contract_prerequisite_effect.container.container_ticket_contract_prerequisite_effect import (
    create_handler_ticket_contract_prerequisite,
)
from omnimarket.nodes.node_ticket_contract_prerequisite_effect.models.model_ticket_contract_prerequisite_request import (
    ModelTicketContractPrerequisiteRequest,
)


def _default_head() -> str:
    """The hook-run ref when this repository holds it, else HEAD.

    A lab pre-check exports ``PRE_COMMIT_TO_REF`` naming a commit of its own worktree to every
    hook it runs; in another repository that ref does not resolve and the lookup exits 128
    (OMN-23283).
    """
    ref = os.environ.get("PRE_COMMIT_TO_REF")
    if ref:
        found = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            capture_output=True,
            check=False,
        )
        if found.returncode == 0:
            return ref
    return "HEAD"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--head", default=_default_head())
    parser.add_argument("--base", default="origin/dev")
    parser.add_argument("--title")
    args = parser.parse_args()
    result = create_handler_ticket_contract_prerequisite().handle(
        ModelTicketContractPrerequisiteRequest(
            head=args.head, base=args.base, title=args.title
        )
    )
    for failure in result.failures:
        sys.stderr.write(failure + "\n")
    return int(not result.passed)


if __name__ == "__main__":
    raise SystemExit(main())
