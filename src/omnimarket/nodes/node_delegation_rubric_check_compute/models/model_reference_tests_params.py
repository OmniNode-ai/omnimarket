# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Required contract parameters for generated_tests_pass_reference."""

from pydantic import BaseModel, ConfigDict, Field


class ModelReferenceTestsParams(BaseModel):
    """Validated, caller-supplied criterion configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # A reference run that names fewer generated tests than this is refused.
    min_generated_tests: int = Field(ge=1)
