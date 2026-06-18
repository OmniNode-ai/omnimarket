# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""node_hostile_reviewer prompt builder — re-export of the canonical owner.

The ``build_prompt`` COMPUTE primitive + its models were re-homed to
``omnimarket.review.prompt_builder`` in OMN-13208 (A1). This module re-exports
them and keeps the node-internal ``HandlerPromptBuilder`` RuntimeLocal shim
until the B1 rebuild (OMN-13210) deletes the node.
"""

from __future__ import annotations

from omnimarket.review.prompt_builder import (
    ModelPromptBuilderInput,
    ModelPromptBuilderOutput,
    build_prompt,
)


class HandlerPromptBuilder:
    """RuntimeLocal handler protocol wrapper for prompt builder."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Delegates to build_prompt with a ModelPromptBuilderInput.
        """
        parsed = ModelPromptBuilderInput(**input_data)
        result = build_prompt(parsed)
        return result.model_dump(mode="json")


__all__: list[str] = [
    "HandlerPromptBuilder",
    "ModelPromptBuilderInput",
    "ModelPromptBuilderOutput",
    "build_prompt",
]
