# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One seam of the view_seam_read graph, as its contract entry declares it."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.seam_reading.enum_seam_artifact_kind import EnumSeamArtifactKind
from omnimarket.models.seam_reading.enum_seam_cause import EnumSeamCause
from omnimarket.models.seam_reading.enum_seam_measure_kind import EnumSeamMeasureKind
from omnimarket.models.seam_reading.enum_seam_unit import EnumSeamUnit
from omnimarket.models.seam_reading.model_seam_count import SEAM_ID_PATTERN


class ModelSeamMeasureDeclaration(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    measure_id: str = Field(min_length=1)
    unit: EnumSeamUnit
    kind: EnumSeamMeasureKind = EnumSeamMeasureKind.TRANSIT


class ModelSeamDeclaration(BaseModel):
    """Stations, measures, identity, permitted and forbidden causes, emitters, overlay keys, status."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seam_id: str = Field(pattern=SEAM_ID_PATTERN)
    sender_station: str = Field(min_length=1)
    receiver_station: str = Field(min_length=1)
    measures: tuple[ModelSeamMeasureDeclaration, ...]
    identity: str = Field(min_length=1)
    permits_blocked: tuple[EnumSeamCause, ...] = ()
    permits_refused: tuple[EnumSeamCause, ...] = ()
    forbidden_causes: tuple[EnumSeamCause, ...] = ()
    identity_artifacts: tuple[EnumSeamArtifactKind, ...] = ()
    sender_emitter: str = ""
    receiver_emitter: str = ""
    one_emitter: bool = False
    latency_budget_key: str = ""
    settlement_deadline_key: str = ""
    delivery_window_key: str | None = None
    status: Literal["declared", "measured"] = "declared"

    @classmethod
    def from_graph_node(cls, node: Mapping[str, Any]) -> Self:
        """Read a COMPUTE_GENERIC entry of the graph; the entry's step_config holds the declaration."""
        config: Mapping[str, Any] = node["step_config"]
        permits: Mapping[str, Any] = config.get("permits", {})
        emitters: Mapping[str, Any] = config.get("emitters", {})
        overlay: Mapping[str, Any] = config.get("overlay_keys", {})
        return cls.model_validate(
            {
                "seam_id": config["seam_id"],
                "sender_station": config["sender_station"],
                "receiver_station": config["receiver_station"],
                "measures": config["measures"],
                "identity": config["identity"],
                "permits_blocked": permits.get("blocked", ()),
                "permits_refused": permits.get("refused", ()),
                "forbidden_causes": config.get("forbids", ()),
                "identity_artifacts": config.get("identity_artifacts", ()),
                "sender_emitter": emitters.get("sender", ""),
                "receiver_emitter": emitters.get("receiver", ""),
                "one_emitter": config.get("one_emitter", False),
                "latency_budget_key": overlay.get("latency_budget", ""),
                "settlement_deadline_key": overlay.get("settlement_deadline", ""),
                "delivery_window_key": overlay.get("delivery_window"),
                "status": config.get("status", "declared"),
            }
        )
