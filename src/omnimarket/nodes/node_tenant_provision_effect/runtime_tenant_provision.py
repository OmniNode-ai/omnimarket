# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Entry of ``python -m omnimarket.nodes.node_tenant_provision_effect``.

``--probe`` loads the overlay and builds the store without connecting, and prints what it found. ``--operation`` with
``--input`` (a command as JSON, the payload the bus carries) hands the command to the handler the contract routes it to
and prints the typed answer. Exit 0 when the command ended ``created``, ``already_exists``, ``found``, ``suspended`` or ``already_suspended``; 1 for every
other typed outcome; 3 for a bad input or an overlay that does not load.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_tenant_provision_effect.handlers.handler_tenant_provision import (
    HandlerTenantProvision,
)
from omnimarket.nodes.node_tenant_provision_effect.handlers.handler_tenant_read import (
    HandlerTenantRead,
)
from omnimarket.nodes.node_tenant_provision_effect.handlers.handler_tenant_suspend import (
    HandlerTenantSuspend,
)
from omnimarket.nodes.node_tenant_provision_effect.models.model_tenant_provision import (
    ModelTenantProvisionCommand,
    ModelTenantReadCommand,
    ModelTenantSuspendCommand,
)
from omnimarket.nodes.node_tenant_provision_effect.overlay import (
    build_store,
    load_overlay,
)

_OK_OUTCOMES = frozenset(
    {"created", "already_exists", "found", "suspended", "already_suspended"}
)


async def _run(operation: str, payload: str) -> tuple[str, str]:
    if operation == "provision":
        provisioned = await HandlerTenantProvision().handle(
            ModelTenantProvisionCommand.model_validate_json(payload)
        )
        return provisioned.outcome.value, provisioned.model_dump_json()
    if operation == "suspend":
        suspended = await HandlerTenantSuspend().handle(
            ModelTenantSuspendCommand.model_validate_json(payload)
        )
        return suspended.outcome.value, suspended.model_dump_json()
    read = await HandlerTenantRead().handle(
        ModelTenantReadCommand.model_validate_json(payload)
    )
    return read.outcome.value, read.model_dump_json()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_tenant_provision_effect")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--operation", choices=("provision", "read", "suspend"))
    parser.add_argument("--input", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.probe:
            overlay = load_overlay()
            build_store(overlay)
            sys.stdout.write(
                json.dumps(
                    {
                        "probe": "ready",
                        "lane": overlay.lane,
                        "realms": list(overlay.realms),
                    }
                )
                + "\n"
            )
            return 0
        if args.operation is None or args.input is None:
            sys.stderr.write(
                "INDETERMINATE: pass --probe, or --operation and --input\n"
            )
            return 3
        outcome, answer = asyncio.run(
            _run(args.operation, args.input.read_text(encoding="utf-8"))
        )
    except (NodeOverlayError, ValidationError, OSError, ValueError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}\n")
        return 3
    sys.stdout.write(answer + "\n")
    return 0 if outcome in _OK_OUTCOMES else 1
