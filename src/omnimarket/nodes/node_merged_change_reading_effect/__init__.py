# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Merged-change reading effect node."""

from omnimarket.nodes.node_merged_change_reading_effect.handlers.handler_merged_change_reading_effect import (
    HandlerMergedChangeReadingEffect,
)


class NodeMergedChangeReadingEffect(HandlerMergedChangeReadingEffect):
    """ONEX entrypoint: read a list of merged changes against the running system."""


__all__ = ["HandlerMergedChangeReadingEffect", "NodeMergedChangeReadingEffect"]
