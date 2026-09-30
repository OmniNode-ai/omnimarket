# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""A test selector extracted from an acceptance criterion's falsifier (OMN-20153)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAcFalsifierCommand(BaseModel):
    """One runnable ``uv run pytest`` selector and where the author named it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str = Field(
        ...,
        min_length=1,
        description=(
            "The normalised command, rebuilt from allowlisted tokens only. "
            "Never a slice of the author's text, so no shell metacharacter can "
            "reach the runner."
        ),
    )
    first_path: str = Field(
        ..., min_length=1, description="The first test path the selector names."
    )
    repo_hint: str | None = Field(
        default=None,
        description="A repository the falsifier says it runs in ('in omnibase_x').",
    )


__all__ = ["ModelAcFalsifierCommand"]
