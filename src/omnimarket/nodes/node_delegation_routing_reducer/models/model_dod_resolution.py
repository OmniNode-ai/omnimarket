# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolved DoD bands plus the contract keys that supplied them (OMN-17765)."""

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_dod_band_source import EnumDodBandSource
from omnimarket.enums.enum_requested_response_shape import EnumRequestedResponseShape


class ModelDodResolution(BaseModel):
    """The two DoD bands for a request, and where each one came from.

    ``resolve_task_class_dod_checks`` returns only the bands, which is all its
    callers need to run the gate. This carries the provenance alongside them so a
    verdict can be read back rather than reverse-engineered: without it the shape
    is resolved and then discarded, and only its *effect* -- the resulting tuple
    -- reaches the payload.

    That is not hypothetical. OMN-17765's population had to be split 28/24 by
    inferring which heuristic band appeared, because no field recorded whether an
    override had been applied, and OMN-17879 has to date its own rows against a
    deploy boundary for the same reason.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    deterministic: tuple[str, ...] = Field(
        default=(),
        description="Resolved deterministic band — the hard floor for this request.",
    )
    heuristic: tuple[str, ...] = Field(
        default=(),
        description="Resolved heuristic band — the graded rubric for this request.",
    )
    requested_shape: EnumRequestedResponseShape = Field(
        default=EnumRequestedResponseShape.UNCONSTRAINED,
        description=(
            "Shape the CALLER's prompt declared, or `unconstrained` when it "
            "declared none or no prompt was supplied."
        ),
    )
    deterministic_source: EnumDodBandSource = Field(
        default=EnumDodBandSource.CLASS_DEFINITION_OF_DONE,
        description="Contract key that supplied `deterministic`.",
    )
    heuristic_source: EnumDodBandSource = Field(
        default=EnumDodBandSource.CLASS_DEFINITION_OF_DONE,
        description="Contract key that supplied `heuristic`.",
    )
