# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compaction experiment row; the portable core binding remains its own type."""

from typing import Literal

from omnibase_core.models.claude_code_binding import ModelClientBinding


class ModelCompactionClientBinding(ModelClientBinding):
    """An assignment reader supplies the session's effective arm on this row."""

    hook_event: Literal["session.compact"] = "session.compact"
    arm: Literal["on", "off"] = "on"


def compaction_arm(binding: ModelClientBinding) -> Literal["on", "off"]:
    """Existing portable rows retain their behavior outside an experiment."""
    return binding.arm if isinstance(binding, ModelCompactionClientBinding) else "on"
