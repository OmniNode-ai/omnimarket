# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Required contract parameters for declared_format."""

from pydantic import BaseModel, ConfigDict, Field


class ModelDeclaredFormatParams(BaseModel):
    """Validated, caller-supplied criterion configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_phrases: tuple[str, ...]
    json_phrases: tuple[str, ...]
    fence_allow_phrases: tuple[str, ...]
    one_fence_pattern: str = Field(min_length=1)
    first_line_pattern: str = Field(min_length=1)
    single_word_pattern: str = Field(min_length=1)
    json_choice_pattern: str = Field(min_length=1)
    echo_field_pattern: str = Field(min_length=1)
    facts_object_pattern: str = Field(min_length=1)
