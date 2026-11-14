# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Throughput tick facts effect node (OMN-23734)."""

from omnimarket.nodes.node_throughput_tick_facts_effect.handlers.handler_throughput_tick_facts import (
    HandlerThroughputTickFacts,
)


class NodeThroughputTickFactsEffect(HandlerThroughputTickFacts):
    """ONEX entrypoint for the hourly check's facts read."""


__all__ = ["HandlerThroughputTickFacts", "NodeThroughputTickFactsEffect"]
