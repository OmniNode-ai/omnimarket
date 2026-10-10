# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run reconciliation in process on the host named by its local timer."""

from __future__ import annotations

import argparse
import os
import socket
import sys
from typing import Never

from pydantic import ValidationError

from omnimarket.models.model_host_reconcile import ModelHostReconcileCommand
from omnimarket.nodes.node_host_reconcile_compute.handlers.handler_host_reconcile_compute import (
    HandlerHostReconcileCompute,
)

from .handlers.adapter_publisher import ContractEventPublisher, publish_topics
from .handlers.handler_host_reconcile_effect import HandlerHostReconcileEffect


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError(f"unknown argument: {message}")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--omni-home", dest="workspace_root", default=os.environ.get("OMNI_HOME", "")
    )
    parser.add_argument("--branch", default="dev")
    try:
        args = parser.parse_args(argv)
    except ValueError as exc:
        sys.stderr.write(f"INDETERMINATE: {exc}\n")
        return 3
    if not args.workspace_root:
        sys.stderr.write(
            "INDETERMINATE: OMNI_HOME is not set and --omni-home was not passed.\n"
        )
        return 3
    try:
        step_timeout_s = int(os.environ.get("ONEX_RECONCILE_STEP_TIMEOUT_S", "1800"))
        request = ModelHostReconcileCommand(
            workspace_root=args.workspace_root,
            branch=args.branch,
            mode="check" if args.check else "repair",
            target_host=socket.gethostname(),
            step_timeout_s=step_timeout_s,
            run_timeout_s=int(
                os.environ.get(
                    "ONEX_RECONCILE_RUN_TIMEOUT_S", str(step_timeout_s * 2 + 300)
                )
            ),
            max_holder_age_s=int(
                os.environ.get("ONEX_RECONCILE_MAX_HOLDER_AGE_S", "7200")
            ),
            lock_stale_s=int(
                os.environ.get("ONEX_RECONCILE_LOCK_STALE_SECONDS", "3600")
            ),
            clone_delegate=os.environ.get("ONEX_RECONCILE_CLONE_DELEGATE"),
            venv_delegate=os.environ.get("ONEX_RECONCILE_VENV_DELEGATE"),
            receipt=os.environ.get("ONEX_RECONCILE_RECEIPT"),
            dispatch_venv=os.environ.get("ONEX_DISPATCH_VENV"),
            ledger=os.environ.get("ONEX_LEDGER_PATH", ""),
            alert_command=os.environ.get("ONEX_RECONCILE_ALERT_CMD", ""),
        )
    except (ValueError, ValidationError) as exc:
        sys.stderr.write(f"INDETERMINATE: {exc}\n")
        return 3
    handler = HandlerHostReconcileEffect(
        evaluator=HandlerHostReconcileCompute(),
        publisher=ContractEventPublisher(),
        completed_topic=publish_topics()[1],
        slack_topic=publish_topics()[0],
    )
    result = handler.handle(request)
    for diagnostic in result.diagnostics:
        sys.stderr.write(f"[reconcile-host] {diagnostic}\n")
    sys.stdout.write(result.model_dump_json() + "\n")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
