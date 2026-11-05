# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pre-push preflight effect: run the repository's pre-push hooks before a lane pushes."""

from pathlib import Path

from .handlers.handler_prepush_preflight import HandlerPrepushPreflight


class NodePrepushPreflightEffect(HandlerPrepushPreflight):
    """Declarative entry point for the pre-push preflight."""

    contract_path = Path(__file__).with_name("contract.yaml")
