# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The newest row attributed to one active principal."""

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelActorNewest(BaseModel):
    """A principal cell and its aware timestamp, before classification."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", strict=True, revalidate_instances="always"
    )
    actor: str = Field(..., min_length=1)
    newest_at: AwareDatetime
