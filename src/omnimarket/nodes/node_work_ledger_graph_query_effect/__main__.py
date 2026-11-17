# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one query: a ModelWorkLedgerGraphQueryRequest as JSON on stdin, the result as JSON on stdout.

Exit 0 when the answer is ok, 1 when it is an error answer.
"""

from __future__ import annotations

import sys

from omnimarket.models.work_ledger_graph import ModelWorkLedgerGraphQueryRequest
from omnimarket.nodes.node_work_ledger_graph_query_effect.handlers.handler_work_ledger_graph_query import (
    HandlerWorkLedgerGraphQuery,
)


def main() -> int:
    request = ModelWorkLedgerGraphQueryRequest.model_validate_json(sys.stdin.read())
    result = HandlerWorkLedgerGraphQuery().handle(request)
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
