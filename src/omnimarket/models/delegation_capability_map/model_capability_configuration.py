# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A configuration's stable identity: call parameters plus context strategy (plan 13.10)."""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation.wire.model_attempt_configuration import (
    ModelAttemptConfiguration,
)


class ModelCapabilityConfiguration(BaseModel):
    """The configuration a verdict was measured under, with a content-derived id.

    The id is a digest of the canonical JSON of the parameters and the context
    strategy, so two producers that send the same configuration name the same cell. It is
    derived, never serialized, so a configuration round-trips through the bus.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    parameters: ModelAttemptConfiguration = Field(
        default_factory=ModelAttemptConfiguration
    )

    @property
    def configuration_id(self) -> str:
        canonical = self.parameters.model_dump_json(exclude_none=False)
        return "cfg-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
