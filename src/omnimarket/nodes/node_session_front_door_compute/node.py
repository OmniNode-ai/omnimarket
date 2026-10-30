# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for a session's front door."""

from pathlib import Path

from .handlers.handler_session_front_door import HandlerSessionFrontDoor


class NodeSessionFrontDoorCompute(HandlerSessionFrontDoor):
    """Declarative entry point for the session-start entry and the session operations."""

    contract_path = Path(__file__).with_name("contract.yaml")
