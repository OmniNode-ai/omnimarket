# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Apply a host's GPU power caps from the command line, against the overlay's backend (OMN-24042)."""

import argparse
import sys

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_gpu_power_caps_effect.handlers.handler_gpu_power_caps import (
    HandlerGpuPowerCaps,
    select_backend,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps import (
    EnumGpuPowerCapsOutcome,
    ModelGpuPowerCapsRequest,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps_overlay import (
    NODE_NAME,
    ModelGpuPowerCapsOverlay,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--requested-by", required=True)
    args = parser.parse_args(argv)
    try:
        request = ModelGpuPowerCapsRequest(
            host=args.host, requested_by=args.requested_by
        )
        overlay = load_structured_overlay(NODE_NAME, ModelGpuPowerCapsOverlay)
        result = HandlerGpuPowerCaps(overlay, select_backend(overlay)).handle(request)
    except (ValidationError, NodeOverlayError) as exc:
        sys.stderr.write(f"gpu-power-caps unavailable: {type(exc).__name__}\n")
        return 4
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    if result.outcome is EnumGpuPowerCapsOutcome.OK:
        return 0
    if result.outcome is EnumGpuPowerCapsOutcome.REFUSED:
        return 2
    return 3


if __name__ == "__main__":
    sys.exit(main())
