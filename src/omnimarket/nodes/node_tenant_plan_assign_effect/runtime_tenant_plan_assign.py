# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Entry of ``python -m omnimarket.nodes.node_tenant_plan_assign_effect``.

``--probe`` loads the overlay and builds the store without connecting. ``--input`` (a command as JSON, the payload the
bus carries) hands the command to the handler and prints the typed answer. Exit 0 when the command ended ``assigned``
or ``already_assigned``; 1 for every other typed outcome; 3 for a bad input or an overlay that does not load.
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
from omnimarket.nodes.node_tenant_plan_assign_effect.handlers.handler_tenant_plan_assign import (
    HandlerTenantPlanAssign,
)
from omnimarket.nodes.node_tenant_plan_assign_effect.models.model_tenant_plan_assign import (
    ModelTenantPlanAssignCommand,
)
from omnimarket.nodes.node_tenant_plan_assign_effect.overlay import (
    build_store,
    load_overlay,
)

_OK = frozenset({"assigned", "already_assigned"})


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_tenant_plan_assign_effect")
    parser.add_argument("--probe", action="store_true")
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
        if args.input is None:
            sys.stderr.write("INDETERMINATE: pass --probe or --input\n")
            return 3
        command = ModelTenantPlanAssignCommand.model_validate_json(
            args.input.read_text(encoding="utf-8")
        )
        result = asyncio.run(HandlerTenantPlanAssign().handle(command))
    except (NodeOverlayError, ValidationError, OSError, ValueError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}\n")
        return 3
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0 if result.outcome.value in _OK else 1
