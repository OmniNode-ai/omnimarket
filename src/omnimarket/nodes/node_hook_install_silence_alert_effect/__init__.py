# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reads the hook-event projection and the ledger lane rows, judges install silence, and raises the alarm (OMN-26658)."""

from __future__ import annotations

from omnimarket.nodes.node_hook_install_silence_alert_effect.handlers.handler_hook_install_silence_alert import (
    HandlerHookInstallSilenceAlert,
)

__all__ = ["HandlerHookInstallSilenceAlert", "NodeHookInstallSilenceAlertEffect"]


class NodeHookInstallSilenceAlertEffect(HandlerHookInstallSilenceAlert):
    """ONEX entry-point wrapper for HandlerHookInstallSilenceAlert."""
