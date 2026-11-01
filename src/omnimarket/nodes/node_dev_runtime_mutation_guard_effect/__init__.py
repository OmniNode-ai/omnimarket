# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refuse a dev runtime container recreate, removal or restart outside a deployment or a grant."""

from pathlib import Path

from .handlers.handler_dev_runtime_mutation_guard import HandlerDevRuntimeMutationGuard


class NodeDevRuntimeMutationGuardEffect(HandlerDevRuntimeMutationGuard):
    """ONEX entry point for the local handler."""

    contract_path = Path(__file__).with_name("contract.yaml")


__all__ = ["HandlerDevRuntimeMutationGuard", "NodeDevRuntimeMutationGuardEffect"]
