# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Durably apply S8 repairs before advancing its cursor."""

from .handlers.handler_stop_only_on_withdrawal_effect import (
    HandlerStopOnlyOnWithdrawalEffect,
)


class NodeStopOnlyOnWithdrawalEffect(HandlerStopOnlyOnWithdrawalEffect):
    """Registered ONEX effect node."""


__all__ = ["NodeStopOnlyOnWithdrawalEffect"]
