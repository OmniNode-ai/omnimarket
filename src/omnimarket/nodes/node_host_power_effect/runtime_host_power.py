# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one host power request from the command line, against the overlay's backend (OMN-22906)."""

import argparse
import sys

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_host_power_effect.handlers.handler_host_power import (
    HandlerHostPower,
    select_backend,
)
from omnimarket.nodes.node_host_power_effect.handlers.ilo_port_runtime import (
    build_ilo_port,
)
from omnimarket.nodes.node_host_power_effect.models.model_host_power import (
    EnumHostPowerAction,
    EnumHostPowerBackend,
    EnumHostPowerOutcome,
    ModelHostPowerRequest,
)
from omnimarket.nodes.node_host_power_effect.models.model_host_power_overlay import (
    NODE_NAME,
    ModelHostPowerOverlay,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument(
        "--action", required=True, choices=[a.value for a in EnumHostPowerAction]
    )
    parser.add_argument("--requested-by", required=True)
    parser.add_argument("--approval-ref")
    args = parser.parse_args(argv)
    try:
        request = ModelHostPowerRequest(
            host=args.host,
            action=EnumHostPowerAction(args.action),
            requested_by=args.requested_by,
            approval_ref=args.approval_ref,
        )
        overlay = load_structured_overlay(NODE_NAME, ModelHostPowerOverlay)
        port = (
            build_ilo_port(overlay)
            if overlay.backend is EnumHostPowerBackend.ILO_REDFISH
            else None
        )
        result = HandlerHostPower(overlay, select_backend(overlay, port)).handle(
            request
        )
    except (ValidationError, NodeOverlayError, ValueError) as exc:
        sys.stderr.write(f"host-power unavailable: {type(exc).__name__}\n")
        return 4
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    if result.outcome in {EnumHostPowerOutcome.OK, EnumHostPowerOutcome.NOOP}:
        return 0
    return 2 if result.outcome is EnumHostPowerOutcome.REFUSED else 3


if __name__ == "__main__":
    sys.exit(main())
