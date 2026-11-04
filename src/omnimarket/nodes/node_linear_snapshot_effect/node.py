# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the Linear snapshot writer."""

from pathlib import Path

from .handlers.handler_linear_snapshot import HandlerLinearSnapshot


class NodeLinearSnapshotEffect(HandlerLinearSnapshot):
    """Declarative entry point for the Linear snapshot writer."""

    contract_path = Path(__file__).with_name("contract.yaml")
