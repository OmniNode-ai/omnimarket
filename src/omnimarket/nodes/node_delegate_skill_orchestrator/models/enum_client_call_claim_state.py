# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""States of one client-call claim row (plan TB.3, OMN-21098)."""

from __future__ import annotations

from enum import StrEnum


class EnumClientCallClaimState(StrEnum):
    """Where one client call stands on its claim row.

    The names are the states of the AgentCallAdapter TLA+ model
    (``formal/agent_call_adapter``), so the model and the store read the same.
    """

    CLAIMED = "claimed"
    ADMITTED = "admitted"
    NOT_ADMITTED = "not_admitted"
    OBSERVED = "observed"
    DISPATCH_INTENDED = "dispatch_intended"
    TERMINAL = "terminal"
