# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""S8: StopOnlyOnWithdrawal, scoped by task, revision, lane and CLAIM run."""

from .handlers.handler_stop_only_on_withdrawal import HandlerStopOnlyOnWithdrawal


class NodeStopOnlyOnWithdrawalCompute(HandlerStopOnlyOnWithdrawal):
    """Registered ONEX node."""


__all__ = ["NodeStopOnlyOnWithdrawalCompute"]
