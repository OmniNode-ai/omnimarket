# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Required contract parameters for code_parses."""

from pydantic import BaseModel, ConfigDict


class ModelCodeParsesParams(BaseModel):
    """Validated, caller-supplied criterion configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code_field_names: tuple[str, ...]
    no_fence_phrases: tuple[str, ...]
    python_phrases: tuple[str, ...]
    yaml_phrases: tuple[str, ...]
    json_phrases: tuple[str, ...]
