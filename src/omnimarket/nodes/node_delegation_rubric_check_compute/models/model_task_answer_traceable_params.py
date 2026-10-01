# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract parameters for tool_use's task_answer_traceable (OMN-20233).

The claims_traceable extraction, plus the shapes an agentic answer writes that
are not facts of their own: a trailing punctuation mark, an abbreviation the
file-name pattern reads as a file, an elision (``...``), the command words
of a call the run issued, and a slash-joined keyword pair (``try/except``).
"""

from pydantic import Field

from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_claims_traceable_params import (
    ModelClaimsTraceableParams,
)


class ModelTaskAnswerTraceableParams(ModelClaimsTraceableParams):
    """Every field is required, so a claims_traceable mapping never validates as this one."""

    trailing_punctuation: str = Field(min_length=1)
    ignore_tokens: tuple[str, ...]
    elision_markers: tuple[str, ...] = Field(min_length=1)
    command_argument_names: tuple[str, ...] = Field(min_length=1)
    citation_line_tolerance: int = Field(ge=0)
    code_keywords: tuple[str, ...]
