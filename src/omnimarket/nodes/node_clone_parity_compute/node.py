# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for clone parity operations."""

from __future__ import annotations

from pathlib import Path

from .handlers.handler_clone_parity import HandlerCloneParity
from .models.model_clone_parity import (
    ModelCloneParityRequest as ModelCloneParityRequest,
)
from .models.model_clone_parity import ModelCloneParityResult as ModelCloneParityResult


class NodeCloneParityCompute(HandlerCloneParity):
    """Declarative entry point for clone parity operations."""

    contract_path = Path(__file__).with_name("contract.yaml")
