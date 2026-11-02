# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for lane reader provisioning."""

from pathlib import Path

from .handlers.handler_lane_reader_provision import HandlerLaneReaderProvision


class NodeLaneReaderProvisionEffect(HandlerLaneReaderProvision):
    """Declarative entry point; the host boundary, overlay and table are injected."""

    contract_path = Path(__file__).with_name("contract.yaml")
