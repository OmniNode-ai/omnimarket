# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the work-ledger seam counter."""

from pathlib import Path

from .handlers.handler_seam_counter import HandlerSeamCounter


class NodeWorkLedgerSeamCounterCompute(HandlerSeamCounter):
    """Declarative entry point for the seam counter."""

    contract_path = Path(__file__).with_name("contract.yaml")
