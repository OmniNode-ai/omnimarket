# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for lane toolchain provisioning."""

from pathlib import Path

from .handlers.handler_lane_toolchain_provision import HandlerLaneToolchainProvision


class NodeLaneToolchainProvisionEffect(HandlerLaneToolchainProvision):
    """Declarative entry point; the host boundary, overlay and table are injected."""

    contract_path = Path(__file__).with_name("contract.yaml")
