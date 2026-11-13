# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Engine overlay for node_typed_decision_effect (OMN-19432).

The overlay names which decision engine a site uses and, for a local engine,
which local model server serves it and where the site abstains. Nothing here
has a default for an overlay key: a local engine must name its backend and
abstention threshold, and a Jev engine must name neither.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

__all__ = [
    "OVERLAY_ID",
    "RESERVED_REQUEST_FIELDS",
    "EnumTypedDecisionEngine",
    "ModelTypedDecisionEngineOverlay",
]


class EnumTypedDecisionEngine(StrEnum):
    """The decision engines a site may route a typed decision to."""

    JEV = "jev"
    LOCAL = "local"


#: Request fields the engine sets itself; the overlay's options never replace them.
RESERVED_REQUEST_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "model",
        "max_tokens",
        "logprobs",
        "top_logprobs",
        "structured_outputs",
        "messages",
    }
)

#: The overlay directory the reader loads this overlay from.
OVERLAY_ID: Final[str] = "node_typed_decision_effect"


class ModelTypedDecisionEngineOverlay(BaseModel):
    """The engine overlay of node_typed_decision_effect. No key has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    engine: EnumTypedDecisionEngine
    local_backend_id: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "The routing-contract backend id of the local model server; this is "
            "how the overlay says WHICH local model serves the decision."
        ),
    )
    local_abstain_below_probability: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="The local engine's abstention threshold.",
    )

    local_request_options: dict[str, JsonValue] | None = Field(
        default=None,
        description=(
            "Extra top-level fields of the local server's chat request, for "
            "example a chat template switch that turns reasoning off. They cannot "
            "replace a field the engine sets."
        ),
    )

    @model_validator(mode="after")
    def _validate_engine_fields(self) -> ModelTypedDecisionEngineOverlay:
        if self.engine is EnumTypedDecisionEngine.LOCAL:
            if (
                self.local_backend_id is None
                or self.local_abstain_below_probability is None
            ):
                raise ValueError(
                    "a local engine names its backend id and abstention threshold"
                )
        elif (
            self.local_backend_id is not None
            or self.local_abstain_below_probability is not None
        ):
            raise ValueError("a Jev engine names no local fields")
        if self.local_request_options is not None:
            if self.engine is not EnumTypedDecisionEngine.LOCAL:
                raise ValueError("a Jev engine names no local fields")
            taken = sorted(set(self.local_request_options) & RESERVED_REQUEST_FIELDS)
            if taken:
                raise ValueError(f"local_request_options cannot replace {taken}")
        return self
