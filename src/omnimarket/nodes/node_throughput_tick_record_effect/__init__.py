# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Throughput tick record effect node (OMN-23734)."""

from omnimarket.nodes.node_throughput_tick_record_effect.handlers.handler_throughput_tick_record import (
    HandlerThroughputTickRecord,
)


class NodeThroughputTickRecordEffect(HandlerThroughputTickRecord):
    """ONEX entrypoint for the hourly check's record step."""


__all__ = ["HandlerThroughputTickRecord", "NodeThroughputTickRecordEffect"]
