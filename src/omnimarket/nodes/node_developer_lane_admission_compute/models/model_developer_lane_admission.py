# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and decision of node_developer_lane_admission_compute (OMN-21599)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue


class ModelDeveloperLaneFacts(BaseModel):
    """What the hook read for one lane of the hook-edge contract.

    ``declaration`` is the lane's own principal declaration document, parsed, or ``None``
    when none is installed. ``env_principal`` is the login name the operator env file holds
    for the lane, read only for a lane whose credential source is that file.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    credential_source: Literal["lane_store", "operator_env", "none"]
    env_principal: str | None = None
    declaration: dict[str, JsonValue] | None = None


class ModelDeveloperLaneAdmissionRequest(BaseModel):
    """The facts a hook hands over; every lane name comes from the hook-edge contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    declared_lane: str
    barred_lanes: tuple[str, ...] = ()
    lane_store: dict[str, JsonValue] | None = None
    lanes: dict[str, ModelDeveloperLaneFacts]


class ModelDeveloperLaneAdmissionResult(BaseModel):
    """The selected lane and the developer's standing on it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    selected_lane: str
    verdict: Literal["operator", "collaborator", "refused"]
    refusal: (
        Literal[
            "binding-invalid",
            "binding-unknown-lane",
            "declaration-malformed",
            "bound-lane-declaration-missing",
            "no-declared-principal",
            "collaborator-on-barred-lane",
            "principal-declared-for-another-lane",
        ]
        | None
    ) = None
    fault: Literal["config", "credential"] | None = None
    detail: str = ""


__all__ = [
    "ModelDeveloperLaneAdmissionRequest",
    "ModelDeveloperLaneAdmissionResult",
    "ModelDeveloperLaneFacts",
]
