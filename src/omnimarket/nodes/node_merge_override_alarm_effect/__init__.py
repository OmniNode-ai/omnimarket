# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Merges made outside the landing nodes raise a typed error and an alarm unless an override names them."""

from __future__ import annotations

from omnimarket.nodes.node_merge_override_alarm_effect.handlers.handler_merge_override_alarm import (
    HandlerMergeOverrideAlarm,
)

__all__ = ["HandlerMergeOverrideAlarm", "NodeMergeOverrideAlarmEffect"]


class NodeMergeOverrideAlarmEffect(HandlerMergeOverrideAlarm):
    """ONEX entry-point wrapper for HandlerMergeOverrideAlarm."""
