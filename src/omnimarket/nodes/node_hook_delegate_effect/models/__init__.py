# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for node_hook_delegate_effect."""

from .model_hook_delegate import (
    EnumDelegateCliState,
    EnumHookDelegateOutcome,
    ModelDelegateCliRun,
    ModelHookDelegateRequest,
    ModelHookDelegateResult,
)
from .model_prompt_delegate import (
    EnumPromptDelegateOutcome,
    ModelPromptDelegateRequest,
    ModelPromptDelegateResult,
)

__all__ = [
    "EnumDelegateCliState",
    "EnumHookDelegateOutcome",
    "EnumPromptDelegateOutcome",
    "ModelDelegateCliRun",
    "ModelHookDelegateRequest",
    "ModelHookDelegateResult",
    "ModelPromptDelegateRequest",
    "ModelPromptDelegateResult",
]
