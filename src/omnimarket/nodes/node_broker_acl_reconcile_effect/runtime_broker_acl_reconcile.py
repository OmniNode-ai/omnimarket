# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Operator entry point: dry run, then apply, of the contract's declared ACL rows.

uv run python -m omnimarket.nodes.node_broker_acl_reconcile_effect --mode dry_run
"""

from __future__ import annotations

import argparse
import shlex
import sys

from omnimarket.models.broker_acl_reconcile import ModelBrokerAclReconcileRequest
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.handler_broker_acl_reconcile import (
    HandlerBrokerAclReconcile,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("dry_run", "apply"), required=True)
    parser.add_argument(
        "--reconcile-command",
        default="",
        help="override the contract's reconcile command (shell-quoted)",
    )
    args = parser.parse_args(argv)
    result = HandlerBrokerAclReconcile().handle(
        ModelBrokerAclReconcileRequest(
            mode=args.mode, reconcile_command=tuple(shlex.split(args.reconcile_command))
        )
    )
    sys.stdout.write(result.output)
    if result.error:
        sys.stderr.write(result.error + "\n")
    return result.exit_code


__all__ = ["main"]
