# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay document of node_dev_roll_decision_request_effect.

Every bound is an overlay value: the issuer pattern, the shadow roll's assumed
duration and the seen-job bound. There is no packaged fallback.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelDevRollInMemoryObservationStore(BaseModel):
    """The in-memory observation store configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["in_memory"]


class ModelDevRollFileObservationStore(BaseModel):
    """The file observation store configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["file"]
    home_env: str = Field(min_length=1)
    relpath: str = Field(min_length=1)


class ModelDevRollDecisionRequestPolicy(BaseModel):
    """The overlay document of node_dev_roll_decision_request_effect."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["shadow"]
    issuer_repository_pattern: str = Field(min_length=1)
    shadow_roll_duration_seconds: int = Field(gt=0)
    seen_job_ids_kept: int = Field(gt=0)
    store: Annotated[
        ModelDevRollInMemoryObservationStore | ModelDevRollFileObservationStore,
        Field(discriminator="backend"),
    ]

    @model_validator(mode="after")
    def _issuer_pattern_naming_a_repository(self) -> ModelDevRollDecisionRequestPolicy:
        pattern = self.issuer_repository_pattern
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise ValueError(
                f"issuer_repository_pattern is not a regular expression ({exc})"
            ) from exc
        if "repository" not in compiled.groupindex:
            raise ValueError(
                "issuer_repository_pattern must carry a named group 'repository'"
            )
        return self
