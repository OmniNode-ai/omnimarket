# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Git merge driver entry: ``python -m omnimarket.nodes.node_ticket_contract_merge_compute %O %A %B %P``.

Writes the merge to ``%A`` and exits 0, or leaves it alone, prints every refusal and exits 1, which
git reads as a conflict.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from omnimarket.nodes.node_ticket_contract_merge_compute.handlers.handler_ticket_contract_merge import (
    HandlerTicketContractMerge,
)
from omnimarket.nodes.node_ticket_contract_merge_compute.models import (
    EnumTicketContractMergeOutcome,
    ModelTicketContractMergeRequest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", type=Path)
    parser.add_argument("current", type=Path)
    parser.add_argument("incoming", type=Path)
    parser.add_argument("source", nargs="?", default="contract")
    args = parser.parse_args(argv)
    result = HandlerTicketContractMerge().handle(
        ModelTicketContractMergeRequest(
            base_text=args.base.read_text(encoding="utf-8"),
            current_text=args.current.read_text(encoding="utf-8"),
            incoming_text=args.incoming.read_text(encoding="utf-8"),
            source=args.source,
        )
    )
    if result.outcome is EnumTicketContractMergeOutcome.MERGED:
        args.current.write_text(result.merged_text, encoding="utf-8")
        return 0
    for refusal in result.refusals:
        sys.stderr.write(
            f"{args.source}: {refusal.code.value} {refusal.section} {refusal.entry}: {refusal.detail}\n"
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
