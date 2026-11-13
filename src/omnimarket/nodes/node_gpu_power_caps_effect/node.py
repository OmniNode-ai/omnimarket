# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for node_gpu_power_caps_effect."""

from pathlib import Path

from .handlers.handler_gpu_power_caps import HandlerGpuPowerCaps


class NodeGpuPowerCapsEffect(HandlerGpuPowerCaps):
    """Contract-defined GPU power caps node."""

    contract_path = Path(__file__).with_name("contract.yaml")
