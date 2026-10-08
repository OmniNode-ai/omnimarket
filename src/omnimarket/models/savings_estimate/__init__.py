# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared vocabulary of the transcript-based savings estimate (OMN-19979).

node_claude_code_history_read_effect turns a developer's own Claude Code
history into :class:`ModelClaudeCodePromptRecord` rows, and
node_savings_estimate_compute turns those into :class:`ModelSavingsEstimateRow`
rows. The models live here, not in either node, because both nodes and the
caller that composes them speak them.

An estimate row is typed ``estimated`` and nothing else, and every money field
on it is named ``estimated_*``: no model in this package can be read as a
measured run, and no measured fold accepts one.
"""

from __future__ import annotations

from omnimarket.models.savings_estimate.model_savings_estimate import (
    ESTIMATE_CSV_COLUMNS,
    EnumSavingsBasis,
    ModelClaudeCodePromptRecord,
    ModelEstimateDelegateRoute,
    ModelSavingsEstimateRow,
)

__all__ = [
    "ESTIMATE_CSV_COLUMNS",
    "EnumSavingsBasis",
    "ModelClaudeCodePromptRecord",
    "ModelEstimateDelegateRoute",
    "ModelSavingsEstimateRow",
]
