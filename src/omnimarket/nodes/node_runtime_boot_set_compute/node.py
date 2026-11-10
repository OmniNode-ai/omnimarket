# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the declared boot set judgment."""

from pathlib import Path

from .handlers.handler_runtime_boot_set import HandlerRuntimeBootSet


class NodeRuntimeBootSetCompute(HandlerRuntimeBootSet):
    """Declarative entry point for runtime boot set operations."""

    contract_path = Path(__file__).with_name("contract.yaml")
