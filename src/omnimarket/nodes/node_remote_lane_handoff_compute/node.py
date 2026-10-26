# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for remote-lane handoff decisions (OMN-20864)."""

from pathlib import Path

from .handlers import HandlerRemoteLaneHandoff


class NodeRemoteLaneHandoffCompute(HandlerRemoteLaneHandoff):
    """Locate a lane's stamped CLAIM or decide whether its handoff can retry."""

    contract_path = Path(__file__).with_name("contract.yaml")
