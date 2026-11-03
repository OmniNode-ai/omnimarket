# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Node runtime: read one JSON walk request, run the walk, print the typed receipt."""

from __future__ import annotations

import argparse
import sys

from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.handlers.handler_onboarding_clean_vm_walk import (
    HandlerOnboardingCleanVmWalk,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.handlers.ssh_tart_runner import (
    HandlerLocalTart,
    HandlerSshTart,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.models.model_clean_vm_walk import (
    ModelWalkRequest,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="JSON ModelWalkRequest")
    args = parser.parse_args()
    request = ModelWalkRequest.model_validate_json(args.input)
    runner = (
        HandlerLocalTart()
        if request.tart_transport == "local"
        else HandlerSshTart(request.tart_host)
    )
    receipt = HandlerOnboardingCleanVmWalk(runner).handle(request)
    sys.stdout.write(receipt.model_dump_json() + "\n")
    return 0
