# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared review COMPUTE primitives for omnimarket review nodes.

OWNER package (OMN-13208 / A1 re-home) for the prompt-builder and
response-parser pure COMPUTE primitives shared across review nodes
(node_hostile_reviewer, node_pr_review_bot). No I/O; deterministic transforms.
"""

from omnimarket.review.prompt_builder import (
    ModelPromptBuilderInput,
    ModelPromptBuilderOutput,
    build_prompt,
)
from omnimarket.review.response_parser import (
    EnumParseStatus,
    ModelParseResult,
    parse_model_response,
)

__all__: list[str] = [
    "EnumParseStatus",
    "ModelParseResult",
    "ModelPromptBuilderInput",
    "ModelPromptBuilderOutput",
    "build_prompt",
    "parse_model_response",
]
