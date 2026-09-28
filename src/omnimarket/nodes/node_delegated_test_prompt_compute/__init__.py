# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegated test prompt compute node.

Pure COMPUTE: builds the WRITE and REPAIR prompts of the delegated test
loop from the criterion, the target excerpt, the previous test and its
failure digest, with the response contract {test_path, test_source}. A
bundle carrying a forbidden fragment (a hidden test's name) is refused.
"""

from omnimarket.nodes.node_delegated_test_prompt_compute.handlers.handler_delegated_test_prompt import (
    DelegatedTestPromptRefusedError,
    HandlerDelegatedTestPrompt,
    build_prompt_bundle,
)
from omnimarket.nodes.node_delegated_test_prompt_compute.models.model_delegated_test_prompt import (
    ModelDelegatedTestPromptBundle,
    ModelDelegatedTestPromptRequest,
    ModelFailureContext,
)


class NodeDelegatedTestPromptCompute(HandlerDelegatedTestPrompt):
    """ONEX entry-point wrapper for HandlerDelegatedTestPrompt."""


__all__ = [
    "DelegatedTestPromptRefusedError",
    "HandlerDelegatedTestPrompt",
    "ModelDelegatedTestPromptBundle",
    "ModelDelegatedTestPromptRequest",
    "ModelFailureContext",
    "NodeDelegatedTestPromptCompute",
    "build_prompt_bundle",
]
