# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for principal-bound session checkpoints."""

from pathlib import Path

from .handlers.handler_session_checkpoint import HandlerSessionCheckpoint


class NodeSessionCheckpointCompute(HandlerSessionCheckpoint):
    """Declarative entry point for session checkpoint operations."""

    contract_path = Path(__file__).with_name("contract.yaml")
