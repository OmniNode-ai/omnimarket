# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23170: a probe-ladder rung may declare that its credential is absent.

The released ``ModelProbeLadder`` and ``ModelProbeRung`` keep exactly their
keys (the Wire Compatibility Gate pins them against the last release). This
module declares new classes only: a rung that can say its declared key
resolves to nothing, and a ladder of such rungs. The walk never selects an
absent-credential rung and makes no attempt on it (OMN-23170 / OMN-18696).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.delegation.wire.model_probe_ladder import ModelProbeRung


class ModelProbeCredentialRung(ModelProbeRung):
    """A probe rung that may declare its credential absent (OMN-23170)."""

    credential_resolved: bool = Field(
        default=True,
        description=(
            "False declares that the key this rung would need resolves to "
            "nothing: the walk never selects it and makes no attempt on it "
            "(OMN-23170)."
        ),
    )


class ModelProbeCredentialLadder(BaseModel):
    """A rungs-mode ladder whose rungs may declare an absent credential."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rungs: tuple[ModelProbeCredentialRung, ...] = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_distinct_backends(self) -> ModelProbeCredentialLadder:
        seen: set[str] = set()
        for rung in self.rungs:
            if rung.backend_id in seen:
                raise ValueError(f"probe ladder repeats backend id {rung.backend_id!r}")
            seen.add(rung.backend_id)
        return self

    @model_validator(mode="after")
    def validate_at_least_one_resolved(self) -> ModelProbeCredentialLadder:
        if not any(rung.credential_resolved for rung in self.rungs):
            raise ValueError("probe ladder has no rung with a resolved credential")
        return self
