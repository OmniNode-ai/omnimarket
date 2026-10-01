# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Required contract parameters for ids_traceable.

diff_scope names which lines of a unified-diff answer are scored:
"whole_answer" or "added_lines".
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelIdsTraceableParams(BaseModel):
    """Validated, caller-supplied criterion configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id_pattern: str = Field(min_length=1)
    diff_scope: Literal["whole_answer", "added_lines"]
