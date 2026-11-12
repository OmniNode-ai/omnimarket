# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Operator entry point: dry run, then apply, of the contract's declared ACL rows.

uv run python -m omnimarket.nodes.node_broker_acl_reconcile_effect --mode dry_run
"""

from __future__ import annotations

import argparse
import sys

from omnimarket.models.broker_acl_reconcile import ModelBrokerAclReconcileRequest
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.handler_broker_acl_reconcile import (
    HandlerBrokerAclReconcile,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=("dry_run", "apply", "ready_check"), required=True
    )
    parser.add_argument("--node", help="the node a ready_check asks about")
    args = parser.parse_args(argv)
    try:
        request = ModelBrokerAclReconcileRequest(mode=args.mode, node=args.node)
    except ValueError as error:
        parser.error(str(error))
    result = HandlerBrokerAclReconcile().handle(request)
    sys.stdout.write(result.output)
    sys.stdout.write(result.model_dump_json(exclude={"output"}) + "\n")
    if result.error:
        sys.stderr.write(result.error + "\n")
    return result.exit_code


__all__ = ["main"]
