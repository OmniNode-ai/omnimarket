# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Required overlay document; every field is a deployment decision."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator


class ModelMergeOverrideSettings(BaseModel):
    """Supplied by the overlay, never packaged."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    landing_app_login: str | None = Field(
        default=None,
        min_length=1,
        description="GitHub login of the landing App; required once any repository is in s19_repositories, judged only there.",
    )
    s19_repositories: list[str] = Field(
        description="owner/name of each repository where S19 is in force (empty before S19)."
    )
    pending_read_timeout_seconds: PositiveInt = Field(
        description="A merge whose GitHub read is unanswered this long is detector_unreadable."
    )
    sweep_interval_seconds: PositiveInt = Field(
        description="At most one pending-read sweep per this many seconds."
    )

    @model_validator(mode="after")
    def _login_required_where_s19_is_in_force(self) -> ModelMergeOverrideSettings:
        if self.s19_repositories and self.landing_app_login is None:
            raise ValueError(
                "landing_app_login is required once any repository is in s19_repositories"
            )
        return self
