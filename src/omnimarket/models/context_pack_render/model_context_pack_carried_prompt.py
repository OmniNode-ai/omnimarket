# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A captured operator prompt carried by the pack (OMN-21109, decision D10)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelContextPackCarriedPrompt(BaseModel):
    """prompt_id is the id the operator would name in a review row.

    cited: a recorded row cites the prompt. in_reviewed_range: a recorded operator review
    row names it. A prompt that is neither is uncited and listed in the trailer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_id: str = Field(min_length=1)
    capture_sequence: int = Field(ge=0)
    cited: bool = False
    in_reviewed_range: bool = False
