# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request and result of the federation edge decision compute.

The request carries everything the decision reads, whole: the runtime's
recorded events, the edge overlay they are decided under, the connection's
identity and lane declaration, the topic registry entry, the ACL probe
record and the decision's own ``now``. The decision itself does no I/O and
reads no clock, so every one of those inputs is required to arrive; an
absent input is a named refusal in the handler, never a default here.

The result is one decision per recorded event: pass, with the record as it
may leave the wire, or refuse, with one named reason. The gate refusal
names (``oversize`` and its siblings) are shared with the egress gate's
vocabulary, so an edge refusal and the gate refusal behind it carry the
same word.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.nodes.node_federation_edge_decision_compute.models.enum_edge_decision import (
    EnumEdgeRefusal,
)
from omnimarket.nodes.node_federation_edge_decision_compute.models.model_edge_overlay import (
    ModelEdgeOverlayFile,
)
from omnimarket.nodes.node_federation_egress_gate_compute.models.enum_egress_gate import (
    EnumEgressOutcome,
)
from omnimarket.nodes.node_federation_egress_gate_compute.models.model_egress_gate import (
    ModelEgressField,
    ModelWireHeader,
)


def _require_timezone_aware(value: datetime) -> datetime:
    """Return ``value``, or raise ``ValueError`` when it is not tz-aware."""
    if value.utcoffset() is None:
        raise ValueError("the datetime must be timezone-aware")
    return value


class ModelRecordedEvent(BaseModel):
    """One event the in-memory runtime recorded, as base64, with its headers.

    ``value_b64`` is the event's bytes and ``key_b64`` its optional key
    bytes; neither is decoded here, only carried whole.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(..., min_length=1)
    key_b64: str | None = None
    value_b64: str
    headers: tuple[ModelWireHeader, ...] = ()


class ModelLaneDeclaration(BaseModel):
    """One lane as the deployment declares it: its auth and its grants.

    ``auth_mechanism`` names the mechanism the lane's connections must have
    authenticated with (``None``: the lane declares none);
    ``granted_write_principals`` is the whole set of principals granted to
    write on it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(..., min_length=1)
    auth_mechanism: str | None
    granted_write_principals: tuple[str, ...]


class ModelAclProbeStep(BaseModel):
    """One step of the ACL probe, with the expected and the observed verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step: Literal["a", "b", "c", "d", "e"]
    expected: Literal["allowed", "denied"]
    observed: Literal["allowed", "denied"]


class ModelAclProbeRecord(BaseModel):
    """The recorded ACL probe the decision reads, with when it was taken.

    ``recorded_at`` must be timezone-aware: the decision reads no clock, so
    staleness is compared against the request's ``now`` and an unaware stamp
    cannot be compared at all.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_ref: str = Field(..., min_length=1)
    lane: str = Field(..., min_length=1)
    principal_ref: str = Field(..., min_length=1)
    recorded_at: datetime
    steps: tuple[ModelAclProbeStep, ...]

    @field_validator("recorded_at")
    @classmethod
    def _recorded_at_is_aware(cls, value: datetime) -> datetime:
        return _require_timezone_aware(value)


class ModelRegistryTopicEntry(BaseModel):
    """The topic registry's one entry: its classes, group and value shape.

    ``fields`` is the topic's closed value shape, exactly the egress gate's
    field model, so the edge decides against the same shape the gate does.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(..., min_length=1)
    classes: tuple[str, ...]
    atomic_group: bool
    fields: tuple[ModelEgressField, ...]


class ModelEdgeDecisionRequest(BaseModel):
    """Everything one edge decision reads. No field has a default.

    ``now`` is the moment the decision is made for, supplied by the caller
    (the decision reads no clock); an omitted ``now`` is refused by the
    handler as ``now-absent``, never defaulted here. When given, it must be
    timezone-aware, as must the ACL probe's ``recorded_at``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    recording: tuple[ModelRecordedEvent, ...]
    overlay: ModelEdgeOverlayFile
    authenticated_identity: str | None
    connection_mechanism: str | None
    lane_declaration: ModelLaneDeclaration | None
    registry_entry: ModelRegistryTopicEntry | None
    acl_probe: ModelAclProbeRecord | None
    now: datetime | None

    @field_validator("now")
    @classmethod
    def _now_is_aware_when_given(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return value
        return _require_timezone_aware(value)


class ModelRecordOut(BaseModel):
    """One record as it may leave the edge, as base64, with its headers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(..., min_length=1)
    key_b64: str
    value_b64: str
    headers: tuple[ModelWireHeader, ...]


class ModelEdgeDecision(BaseModel):
    """The decision's answer for one recorded event.

    A pass carries ``record_out`` (the record as it may leave) and no
    reason; a refusal carries exactly one named reason and never a record.
    ``not_selected`` counts the recording's other events this decision did
    not select, so one run's answers account for its whole recording.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumEgressOutcome
    reason: EnumEdgeRefusal | None = None
    record_out: ModelRecordOut | None = None
    not_selected: int = Field(..., ge=0)
    topic: str | None = None
    key_digest: str | None = None
    message_id: str | None = None

    @model_validator(mode="after")
    def _reason_agrees_with_outcome(self) -> ModelEdgeDecision:
        if self.outcome is EnumEgressOutcome.PASS:
            if self.reason is not None:
                raise ValueError("a pass decision carries no refusal reason")
            if self.record_out is None:
                raise ValueError("a pass decision carries its record_out")
        else:
            if self.reason is None:
                raise ValueError("a refuse decision names one refusal reason")
            if self.record_out is not None:
                raise ValueError("a refuse decision carries no record_out")
        return self


__all__: list[str] = [
    "ModelAclProbeRecord",
    "ModelAclProbeStep",
    "ModelEdgeDecision",
    "ModelEdgeDecisionRequest",
    "ModelLaneDeclaration",
    "ModelRecordOut",
    "ModelRecordedEvent",
    "ModelRegistryTopicEntry",
]
