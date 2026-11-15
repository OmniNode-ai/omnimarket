# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The edge overlay of the federation edge decision compute.

One in-memory runtime hands its recorded events to one lane through one
topic. The overlay is that whole declaration: the runtime binding it acts
for, the edge's own id, and the one ``up`` direction with its source, its
lane destination, the topic it serves, the wire limits it enforces and the
receipt effect it owes. Nothing here has a default: an absent key refuses
the load, and a cross-field disagreement (a runtime ref the source does not
name, a lane the binding does not permit, a pinned topic the one resolver
does not answer) refuses it too.
"""

from __future__ import annotations

from typing import Literal

from omnibase_infra.nodes.node_bus_forwarder_effect.services.service_gateway_topic_transform import (
    resolve_physical_topic,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.models.federation_egress.model_egress_gate import (
    ModelHeaderRule,
    assert_bounded_anchored_pattern,
)


class ModelRuntimeBinding(BaseModel):
    """The runtime and principal the edge acts for, with the lanes it may use.

    ``runtime_ref`` names the in-memory runtime whose recorded events this
    edge carries; ``principal_ref`` the principal it authenticates to the
    lane with; ``tenant_slug_ref`` the tenant slug the wire topic is pinned
    under. ``origin_lane`` is the lane the runtime itself is reached on, and
    ``permitted_lanes`` the whole set a destination lane may come from.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime_ref: str = Field(..., min_length=1)
    principal_ref: str = Field(..., min_length=1)
    tenant_slug_ref: str = Field(..., min_length=1)
    origin_lane: str = Field(..., min_length=1)
    permitted_lanes: tuple[str, ...] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _origin_is_permitted(self) -> ModelRuntimeBinding:
        if len(set(self.permitted_lanes)) != len(self.permitted_lanes):
            raise ValueError("permitted_lanes entries are not unique")
        if self.origin_lane not in self.permitted_lanes:
            raise ValueError("origin_lane is not in permitted_lanes")
        return self


class ModelEdgeFrom(BaseModel):
    """The in-memory source the recorded events are read from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["inmemory"]
    runtime_ref: str = Field(..., min_length=1)
    max_events: int = Field(..., ge=1)
    flush_timeout_seconds: float = Field(..., gt=0)


class ModelEdgeTo(BaseModel):
    """The lane the recorded events are carried to, as this bus publishes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["kafka"]
    lane: str = Field(..., min_length=1)
    auth_ref: str = Field(..., min_length=1)
    producer_acks: Literal["all"]
    idempotent_producer: Literal[True]
    acl_probe_ref: str = Field(..., min_length=1)
    acl_probe_max_age_seconds: int = Field(..., ge=1)


class ModelEdgeReceipt(BaseModel):
    """The receipt effect the edge owes: its three topics and its bounds.

    ``effect_class: read_before_act`` and ``item_kind: unit`` are pinned: a
    receipt is waited for before the act is taken, one unit at a time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_topic: str = Field(..., min_length=1)
    record_topic: str = Field(..., min_length=1)
    refusal_topic: str = Field(..., min_length=1)
    wait_seconds: float = Field(..., gt=0)
    in_doubt_scan_max_records: int = Field(..., ge=1)
    in_doubt_max_age_seconds: int = Field(..., ge=1)
    effect_class: Literal["read_before_act"]
    item_kind: Literal["unit"]


class ModelEdgeAcl(BaseModel):
    """The ACL principal refs the edge's topic is measured under."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    write_principal_ref: str = Field(..., min_length=1)
    read_principal_ref: str = Field(..., min_length=1)
    read_group_prefix: str = Field(..., min_length=1)


class ModelStampHeaders(BaseModel):
    """This bus's own three stamp headers, never allowed out on the wire."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    origin_lane: str = Field(..., min_length=1)
    source_lane: str = Field(..., min_length=1)
    hops: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _names_distinct(self) -> ModelStampHeaders:
        names = (self.origin_lane, self.source_lane, self.hops)
        lowered = {name.lower() for name in names}
        if len(lowered) != len(names):
            raise ValueError("the three stamp header names are not distinct")
        return self


class ModelEdgeDirection(BaseModel):
    """The one ``up`` direction: its source, its lane, its topic and its wire.

    The wire limits mirror the egress gate's policy shape -- the key pattern,
    the header allow-list, the value bounds and the stamp headers -- and
    ``pinned_physical_topic`` is the physical topic the record is written to,
    checked against the one runtime resolver when the file loads. No field
    has a default.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=False)

    name: Literal["up"]
    from_: ModelEdgeFrom = Field(..., alias="from")
    to: ModelEdgeTo
    allowed_topic: str = Field(..., min_length=1)
    pinned_physical_topic: str = Field(..., min_length=1)
    key_pattern: str
    key_binding: str = Field(..., min_length=1)
    header_allow_list: tuple[ModelHeaderRule, ...] = Field(..., min_length=1)
    max_headers: int = Field(..., ge=1)
    max_value_bytes: int = Field(..., ge=1)
    max_value_depth: int = Field(..., ge=1)
    stamp_headers: ModelStampHeaders
    message_id_header: str = Field(..., min_length=1)
    classes: tuple[Literal["internal"], ...] = Field(..., min_length=1)
    delivery_mode: Literal["once_per_run"]
    wire_topic_mapping: Literal["identity", "tenant_wire_prefix"]
    receipt: ModelEdgeReceipt
    acl: ModelEdgeAcl

    @field_validator("key_pattern")
    @classmethod
    def _key_pattern_is_bounded(cls, value: str) -> str:
        return assert_bounded_anchored_pattern(
            value, what="ModelEdgeDirection.key_pattern"
        )

    @model_validator(mode="after")
    def _wire_names_agree(self) -> ModelEdgeDirection:
        rules_by_name = {rule.name: rule for rule in self.header_allow_list}
        if len(rules_by_name) != len(self.header_allow_list):
            raise ValueError("header allow-list names are not unique")
        if self.message_id_header not in rules_by_name:
            raise ValueError(
                f"message_id_header {self.message_id_header!r} must name a "
                "header_allow_list rule"
            )
        stamp_lower = {
            self.stamp_headers.origin_lane.lower(),
            self.stamp_headers.source_lane.lower(),
            self.stamp_headers.hops.lower(),
        }
        for rule in self.header_allow_list:
            if rule.name.lower() in stamp_lower:
                raise ValueError(
                    f"header allow-list name {rule.name!r} equals a stamp header name"
                )
        return self


class ModelEdgeOverlayFile(BaseModel):
    """The whole edge overlay file: its binding, its id and its direction.

    A cross-field disagreement refuses the load: the source and the ACL must
    name the binding's refs, the destination lane must be permitted, and the
    pinned physical topic must be exactly what the one runtime topic resolver
    answers for the allowed topic and the binding's tenant slug (a resolver
    refusal refuses the load too), or the allowed topic itself under the
    identity mapping.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    binding: ModelRuntimeBinding
    edge_id: str = Field(..., min_length=1)
    direction: ModelEdgeDirection

    @model_validator(mode="after")
    def _binding_agrees(self) -> ModelEdgeOverlayFile:
        if self.direction.from_.runtime_ref != self.binding.runtime_ref:
            raise ValueError(
                "direction.from.runtime_ref is not the binding's runtime_ref"
            )
        if self.direction.to.auth_ref != self.binding.principal_ref:
            raise ValueError("direction.to.auth_ref is not the binding's principal_ref")
        if self.direction.acl.write_principal_ref != self.binding.principal_ref:
            raise ValueError(
                "direction.acl.write_principal_ref is not the binding's principal_ref"
            )
        if self.direction.to.lane not in self.binding.permitted_lanes:
            raise ValueError("direction.to.lane is not in permitted_lanes")
        if self.direction.wire_topic_mapping == "tenant_wire_prefix":
            try:
                expected = resolve_physical_topic(
                    self.direction.allowed_topic,
                    tenant_slug=self.binding.tenant_slug_ref,
                )
            except ValueError as error:
                raise ValueError(
                    "the topic resolver refused the allowed topic or tenant "
                    f"slug: {error}"
                ) from error
        else:
            expected = self.direction.allowed_topic
        if self.direction.pinned_physical_topic != expected:
            raise ValueError(
                "pinned_physical_topic is not the resolver's answer for the "
                "allowed topic and the binding's tenant slug"
            )
        return self


__all__: list[str] = [
    "ModelEdgeAcl",
    "ModelEdgeDirection",
    "ModelEdgeFrom",
    "ModelEdgeOverlayFile",
    "ModelEdgeReceipt",
    "ModelEdgeTo",
    "ModelRuntimeBinding",
    "ModelStampHeaders",
]
