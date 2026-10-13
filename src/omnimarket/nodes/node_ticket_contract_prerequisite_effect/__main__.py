# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local pre-push and CI adapter for the contract-backed prerequisite node."""

from __future__ import annotations

import argparse
import os
import sys

from omnimarket.nodes.node_ticket_contract_prerequisite_effect.handlers.handler_ticket_contract_prerequisite import (
    HandlerTicketContractPrerequisite,
)
from omnimarket.nodes.node_ticket_contract_prerequisite_effect.models.model_ticket_contract_prerequisite_request import (
    ModelTicketContractPrerequisiteRequest,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--head", default=os.environ.get("PRE_COMMIT_TO_REF") or "HEAD")
    parser.add_argument("--base", default="origin/dev")
    parser.add_argument("--title")
    args = parser.parse_args()
    result = HandlerTicketContractPrerequisite().handle(
        ModelTicketContractPrerequisiteRequest(
            head=args.head, base=args.base, title=args.title
        )
    )
    for failure in result.failures:
        sys.stderr.write(failure + "\n")
    return int(not result.passed)


if __name__ == "__main__":
    raise SystemExit(main())
