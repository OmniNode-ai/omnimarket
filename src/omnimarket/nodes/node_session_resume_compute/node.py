# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for member-scoped session recovery over the rolling ledger."""

from pathlib import Path

from .handlers.handler_session_resume import HandlerSessionResume


class NodeSessionResumeCompute(HandlerSessionResume):
    """Declarative entry point for session recovery operations."""

    contract_path = Path(__file__).with_name("contract.yaml")
