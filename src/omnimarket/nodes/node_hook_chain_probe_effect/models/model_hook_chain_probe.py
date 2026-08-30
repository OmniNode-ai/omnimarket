# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed surface for the hook->cloud chain probe (OMN-17202).

Five legs, one correlation id. Every ticket on this chain carried a proof of its
own leg and none carried a proof of the union, so the chain was green-by-parts
and dead-in-fact from 2026-08-21. These models are the union's vocabulary:
structural OBSERVATIONS (what each leg reported, no judgement) and the typed
RESULT (which leg it died at, and why).

The blocker vocabulary is deliberately specific. ``TIMEOUT`` is a last resort,
never the answer when a structural fact already explains the stall -- a probe
that cannot tell "denied by the forwarder allowlist" apart from "admitted but
nothing is consuming" is not a diagnosis, it is a restatement of the symptom.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumHookChainLeg(StrEnum):
    """The five legs of the hook->cloud chain, in traversal order."""

    LOCAL_EMIT = "local_emit"
    LOCAL_BUS = "local_bus"
    FORWARDER_RELAY = "forwarder_relay"
    CLOUD_GATEWAY = "cloud_gateway"
    CLOUD_PROJECTION = "cloud_projection"


#: Traversal order. The probe walks this and stops at the first leg not reached.
LEG_ORDER: tuple[EnumHookChainLeg, ...] = (
    EnumHookChainLeg.LOCAL_EMIT,
    EnumHookChainLeg.LOCAL_BUS,
    EnumHookChainLeg.FORWARDER_RELAY,
    EnumHookChainLeg.CLOUD_GATEWAY,
    EnumHookChainLeg.CLOUD_PROJECTION,
)


class EnumHookChainBlocker(StrEnum):
    """Why a leg was not reached.

    ``NOT_ATTEMPTED`` is distinct from every failure: a leg downstream of the
    break was never probed, so reporting it as failing would invent evidence.
    """

    NONE = "none"
    NOT_ATTEMPTED = "not_attempted"
    HOOK_LANE_UNRESOLVED = "hook_lane_unresolved"
    EMIT_REFUSED = "emit_refused"
    NOT_ON_BUS = "not_on_bus"
    ALLOWLIST_DENIED = "allowlist_denied"
    LANE_MISMATCH = "lane_mismatch"
    TRANSPORT_NOT_RELAY = "transport_not_relay"
    NO_CONSUMER = "no_consumer"
    GATEWAY_UNREACHABLE = "gateway_unreachable"
    GATEWAY_UNAUTHORIZED = "gateway_unauthorized"
    GATEWAY_INGEST_ABSENT = "gateway_ingest_absent"
    PROJECTION_ROW_ABSENT = "projection_row_absent"
    TIMEOUT = "timeout"


class ModelHookChainAddress(BaseModel):
    """Runtime-resolved addressing for one probe run.

    Resolved from contracts at the effect boundary -- the probe never hardcodes
    a topic, a lane, or a URL (ticket scope note; feedback_addressing_is_runtime_resolved).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    hook_topic: str = Field(..., description="Hook event topic the probe emits on.")
    emit_lane: str = Field(
        ...,
        description="The hook EDGE lane -- the broker the hooks themselves publish to.",
    )
    emit_lane_authority: str = Field(
        ...,
        description="Which authority declared emit_lane. Recorded because reading "
        "the ambient shell instead of the hooks' own env authority is the "
        "OMN-17010 defect that produced a wrong-broker verdict on OMN-16162.",
    )
    cloud_gateway_base_url: str = Field(
        ..., description="Cloud gateway base URL for the leg 4/5 read routes."
    )


class ModelLocalEmitObservation(BaseModel):
    """Leg 1: did the correlated event leave the operator machine."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    emitted: bool
    lane: str
    topic: str
    lane_resolved: bool = Field(
        default=True,
        description="False when no hook env authority declared the hook edge lane. "
        "Distinct from a refused emit: nothing was attempted, because guessing "
        "a lane is the failure mode this probe exists to end.",
    )
    detail: str | None = None


class ModelLocalBusObservation(BaseModel):
    """Leg 2: was the correlated record readable back off the local bus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observed: bool
    lane: str
    topic: str
    offset: int | None = None
    detail: str | None = None


class ModelForwarderObservation(BaseModel):
    """Leg 3: the forwarder's contract-declared policy and its live consumption.

    The first two fields are POLICY read from the forwarder's own contract; the
    last two are LIVE facts measured on the hook edge lane. Keeping them
    separate is what lets the classifier say "denied" rather than "timed out".

    ``forwarder_present_on_emit_lane`` replaces an earlier string comparison of
    lane names. The forwarder does not declare its consumed lane in its
    contract and its runtime config loader requires cluster-side paths, so that
    comparison could only ever compare a value against itself -- a blocker that
    can never fire is worse than none, because it reads as a check. The
    measurable fact from the operator Mac is whether the forwarder's own
    contract-declared liveness topic exists and carries records ON the lane the
    hooks publish to.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mirror_outbound_topics: tuple[str, ...] = Field(
        ..., description="Contract-declared outbound mirror allowlist."
    )
    cloud_leg_transport: str = Field(
        ...,
        description="Configured cloud-leg transport: 'https_relay' is canonical; "
        "'kafka' means the direct-MSK leg OMN-16459 retires.",
    )
    liveness_topic: str = Field(
        ...,
        description="The forwarder's own contract-declared liveness/canary topic, "
        "used as the evidence of which lane the forwarder is attached to.",
    )
    forwarder_present_on_emit_lane: bool = Field(
        ...,
        description="Whether liveness_topic carries records on the hook edge lane. "
        "False means the forwarder is not attached to the lane hooks publish to.",
    )
    consumer_group_advanced: bool = Field(
        ...,
        description="Whether the forwarder's consumer group advanced past the record.",
    )
    detail: str | None = None


class ModelCloudGatewayObservation(BaseModel):
    """Leg 4: did the cloud gateway ingest the correlated event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reachable: bool
    status_code: int | None = None
    correlation_found: bool = False
    detail: str | None = None


class ModelCloudProjectionObservation(BaseModel):
    """Leg 5: is the correlated row readable in the cloud projection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reachable: bool
    status_code: int | None = None
    row_found: bool = False
    detail: str | None = None


class ModelHookChainLegResult(BaseModel):
    """One leg's verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    leg: EnumHookChainLeg
    reached: bool
    blocker: EnumHookChainBlocker
    evidence: str = Field(
        default="", description="What was and was not observed at this leg."
    )


class ModelHookChainProbeRequest(BaseModel):
    """One probe invocation.

    Carries no addressing: topic, lane and gateway URL are runtime-resolved from
    contracts. ``correlation_id`` is optional -- the probe mints one when absent,
    which is the normal operator path.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str | None = Field(
        default=None,
        description="Correlation id to trace; minted when omitted.",
    )
    timeout_seconds: float = Field(
        default=30.0, gt=0.0, description="Per-leg observation deadline."
    )


class ModelHookChainProbeResult(BaseModel):
    """The union proof: which leg the correlated event reached, and where it died."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    hook_topic: str
    legs: tuple[ModelHookChainLegResult, ...]
    furthest_leg_reached: EnumHookChainLeg | None = None
    failed_leg: EnumHookChainLeg | None = None
    primary_blocker: EnumHookChainBlocker = EnumHookChainBlocker.NONE
    blockers: tuple[EnumHookChainBlocker, ...] = ()
    chain_complete: bool = False


__all__: list[str] = [
    "LEG_ORDER",
    "EnumHookChainBlocker",
    "EnumHookChainLeg",
    "ModelCloudGatewayObservation",
    "ModelCloudProjectionObservation",
    "ModelForwarderObservation",
    "ModelHookChainAddress",
    "ModelHookChainLegResult",
    "ModelHookChainProbeRequest",
    "ModelHookChainProbeResult",
    "ModelLocalBusObservation",
    "ModelLocalEmitObservation",
]
