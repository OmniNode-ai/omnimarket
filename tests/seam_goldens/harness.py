# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared real-seam driving machinery for the OMN-16004 goldens.

What "real seam" means here, concretely:

* The **producer** is real code or a real committed artifact — the packaged
  ``node_bus_forwarder_effect/contract.yaml`` shipped inside the pinned
  ``omnibase_infra`` wheel, the actual literal constants defined in
  ``omnibase_core`` / ``omnibase_infra``, or the hand-rolled JSON dict literal
  the cloud MSK publisher genuinely emits.
* The **registry** leg is the real ``HandlerSeamMatch`` compute handler from
  ``node_seam_match_compute``, fed the real ``seams.v1.yaml`` row — not a
  re-implementation of its classification rules.
* The **consumer** is real code — ``ServiceGatewayForwarder``, the
  contract-declared gateway handlers, ``ModelDelegateSkillRequest``,
  ``ModelEventEnvelope.model_validate_json``.

Nothing in the seam itself is mocked. :class:`RecordingPublisher` is the only
substituted object and it is deliberately *outside* the seam: it is the
transport sink the forwarder publishes into, the direct analogue of the
``EventBusInmemory`` fixture the existing golden-chain tests drive nodes
through. Capturing the emitted bytes is how the goldens observe the wire; it
replaces a broker, never a seam participant.

Correlation preservation is the bar the ticket sets above shape comparison:
every round-trip golden mints one ``correlation_id`` at the producer and
asserts that exact UUID is observed at the consumer, after a full serialize /
transform / deserialize cycle. A shape match with a dropped correlation id
fails these goldens.

Observed projections — the rule this module enforces
----------------------------------------------------

``HandlerSeamMatch`` earns ``REGENERABLE`` only when legs 2 and 3 (observed
producer vs declared, observed consumer vs declared) are both explicitly
green. That verdict is worth exactly as much as the ``observed_*`` inputs are
independent of the ``declared_*`` ones. Passing the *same projection object*
for both makes the comparison ``x == x``, so ``REGENERABLE`` is guaranteed
whenever leg 1 passes and is insensitive to everything the golden drove. That
tautology shipped in the first cut of these goldens; this module now makes it
impossible:

* :func:`run_registry_match` **rejects** an ``observed_*`` argument that is the
  same object as a ``declared_*`` argument, and
* ``tests/seam_goldens/test_no_tautological_observations.py`` rejects the
  syntactic aliasing pattern (``observed_producer=declared_producer``) across
  every golden module.

An *observed* projection is only honest when every field of it is derived from
an artifact **the test did not author**: bytes a real service published, a real
handler's returned model, a real committed contract file inside the pinned
wheel, or a real imported module constant. Where no such artifact exists for a
side of an edge — the cloud ``onex-api`` publisher and the cloud terminal
consumer are not in this repo's dependency closure — the golden supplies
``None`` for that side, the handler leaves the leg unevaluated, and the edge
classifies as ``SHAPE_ONLY``. ``slice_manifest.yaml`` records that outcome per
edge in ``observation_class`` / ``observation_note``, so a downgrade is a
committed, inspectable fact rather than a silent one.

``envelope_version`` follows the same rule. Models that carry a wire version
(``ModelEventEnvelope.envelope_version``) have it read off the real parsed
instance. Models that carry none (``ModelGatewayEnvelope``,
``ModelDelegateSkillRequest``, ``ModelDispatchRoute``) record the
:data:`UNVERSIONED_MODEL` sentinel on both sides rather than a fabricated
``"1.0.0"`` — the field genuinely carries no signal for those edges and says
so.
"""

from __future__ import annotations

import json
import types
import typing
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache, lru_cache
from pathlib import Path
from typing import Final
from uuid import UUID

import omnibase_infra
import yaml
from omnibase_core.models.core.model_envelope_metadata import ModelEnvelopeMetadata
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.nodes.node_bus_forwarder_effect.models.model_gateway_cloud_bus_config import (
    ModelGatewayCloudBusConfig,
)
from omnibase_infra.nodes.node_bus_forwarder_effect.models.model_gateway_forwarder_config import (
    ModelGatewayForwarderConfig,
)
from omnibase_infra.nodes.node_bus_forwarder_effect.models.model_gateway_mirror_topics import (
    ModelGatewayMirrorTopics,
)
from omnibase_infra.nodes.node_bus_forwarder_effect.models.model_gateway_tenant_identity import (
    ModelGatewayTenantIdentity,
)

from omnimarket.nodes.node_seam_match_compute.handlers.handler_seam_match import (
    HandlerSeamMatch,
)
from omnimarket.nodes.node_seam_match_compute.models.model_seam_match_request import (
    ModelSeamMatchRequest,
)
from omnimarket.nodes.node_seam_match_compute.models.model_seam_match_verdict import (
    ModelSeamMatchVerdict,
)
from omnimarket.seams.models.model_seam_projection import (
    EnumSeamDeliverySemantics,
    EnumSeamProjectionRole,
    ModelSeamProjection,
    ModelSeamProjectionField,
)
from tests.seam_goldens.manifest import (
    REPO_ROOT,
    EnumSeamObservationClass,
    load_slice_manifest,
)

__all__ = [
    "ABSENT_FROM_WIRE",
    "GATEWAY_PRINCIPAL_ID",
    "GATEWAY_TENANT_ID",
    "GATEWAY_TENANT_SLUG",
    "INFRA_GATEWAY_CONTRACT_PATH",
    "SEAM_ENVELOPE_MODEL",
    "SEAM_ENVELOPE_VERSION",
    "UNVERSIONED_MODEL",
    "BusMessage",
    "EnumSeamProjectionRole",
    "RecordingEventBus",
    "RecordingPublisher",
    "annotated_type_name",
    "assert_correlation_preserved",
    "assert_regenerable",
    "assert_registry_classification",
    "assert_shape_only",
    "build_forwarder_config",
    "cloud_hand_rolled_envelope_json",
    "consumer_projection",
    "gateway_cloud_leg",
    "gateway_contract_version",
    "gateway_mirror_topics",
    "load_gateway_contract",
    "local_typed_envelope",
    "model_identity",
    "observed_projection_from_instance",
    "observed_projection_from_mapping",
    "observed_projection_from_model_class",
    "observed_type_name",
    "omnimarket_node_event_bus",
    "producer_projection",
    "registry_edge",
    "registry_header",
    "run_registry_match",
    "wire_body",
]

# ---------------------------------------------------------------------------
# Tenant identity used by every gateway golden.
#
# The principal id is DERIVED from the tenant uuid rather than typed twice:
# ModelGatewayTenantIdentity enforces the canonical ``t-<32 lowercase hex>``
# form, and deriving it means a golden can never assert tenant attribution
# (S5) against two identities that only look consistent.
# ---------------------------------------------------------------------------

GATEWAY_TENANT_ID: Final[UUID] = UUID("6f1d0d3c-9a4e-4d1b-8f27-3a5e6c0b1d42")
GATEWAY_TENANT_SLUG: Final[str] = "acme"
GATEWAY_PRINCIPAL_ID: Final[str] = f"t-{GATEWAY_TENANT_ID.hex}"

# The canonical wire envelope on BOTH broker legs, per the forwarder service's
# own module docstring. Used as the seam-projection envelope identity.
SEAM_ENVELOPE_MODEL: Final[str] = (
    "omnibase_core.models.events.model_event_envelope.ModelEventEnvelope"
)
# ModelEventEnvelope.envelope_version default (ModelSemVer(2, 1, 0)).
SEAM_ENVELOPE_VERSION: Final[str] = "2.1.0"

# The gateway contract as it is actually PACKAGED in the pinned omnibase_infra
# wheel — not a copy vendored into this repo. If the dependency's contract
# changes shape, these goldens read the new shape and fail on the real drift.
INFRA_GATEWAY_CONTRACT_PATH: Final[Path] = (
    Path(omnibase_infra.__file__).parent
    / "nodes"
    / "node_bus_forwarder_effect"
    / "contract.yaml"
)


# ---------------------------------------------------------------------------
# Transport doubles (outside the seam).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordedPublish:
    """One captured publish at the transport boundary."""

    topic: str
    key: bytes | None
    value: bytes

    def envelope(self) -> ModelEventEnvelope[dict[str, object]]:
        """Re-parse the captured bytes exactly as a downstream consumer would.

        Deliberately parses the *bytes*, never a retained in-memory object:
        correlation preservation must survive real serialization, which is the
        step a shape comparison never exercises.
        """

        return ModelEventEnvelope[dict[str, object]].model_validate_json(self.value)


class RecordingPublisher:
    """A ``ProtocolGatewayPublisher`` that captures instead of brokering.

    Structurally satisfies the protocol the real ``ServiceGatewayForwarder``
    publishes through, so the forwarder runs its genuine publish path
    (including the delivery-retry wrapper) with no patching.
    """

    def __init__(self) -> None:
        self.published: list[RecordedPublish] = []

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: object | None = None,
    ) -> None:
        self.published.append(RecordedPublish(topic=topic, key=key, value=value))

    def only(self) -> RecordedPublish:
        """The single publish this leg produced; fails loudly on 0 or 2+."""

        if len(self.published) != 1:
            raise AssertionError(
                f"expected exactly one publish, captured {len(self.published)}: "
                f"{[p.topic for p in self.published]}"
            )
        return self.published[0]

    def topics(self) -> list[str]:
        return [published.topic for published in self.published]


@dataclass(frozen=True)
class RecordedEnvelopePublish:
    """One captured ``publish_envelope`` call at the local event-bus boundary."""

    topic: str
    envelope: ModelEventEnvelope[object]

    def reparsed(self) -> ModelEventEnvelope[dict[str, object]]:
        """Serialize then re-parse, exactly as the local broker leg would.

        The dispatcher hands the bus a live model object; a consumer only ever
        sees bytes. Round-tripping here means the observed projection is built
        from the wire form, not from a retained in-memory object — the same
        discipline :meth:`RecordedPublish.envelope` applies on the broker legs.
        """

        return ModelEventEnvelope[dict[str, object]].model_validate_json(
            self.serialized()
        )

    def serialized(self) -> bytes:
        return self.envelope.model_dump_json(exclude_none=True).encode("utf-8")


class RecordingEventBus:
    """A ``ProtocolEventBus`` publish sink that captures instead of brokering.

    Structurally satisfies the one method the delegation dispatchers call
    (``publish_envelope(envelope, topic=...)``), so ``DispatcherQualityGateResult``
    runs its genuine terminal-emission path with nothing patched. This is the
    S2 analogue of :class:`RecordingPublisher`: a transport sink outside the
    seam, never a seam participant.
    """

    def __init__(self) -> None:
        self.published: list[RecordedEnvelopePublish] = []

    async def publish_envelope(
        self, envelope: ModelEventEnvelope[object], topic: str
    ) -> None:
        self.published.append(RecordedEnvelopePublish(topic=topic, envelope=envelope))

    def only(self) -> RecordedEnvelopePublish:
        """The single envelope publish this leg produced; fails on 0 or 2+."""

        if len(self.published) != 1:
            raise AssertionError(
                f"expected exactly one envelope publish, captured "
                f"{len(self.published)}: {[p.topic for p in self.published]}"
            )
        return self.published[0]

    def topics(self) -> list[str]:
        return [published.topic for published in self.published]


@dataclass(frozen=True)
class BusMessage:
    """The minimal bus-message shape the forwarder reads off a subscription.

    ``ServiceGatewayForwarder`` pulls ``topic`` / ``value`` / ``key`` /
    ``headers`` off the polled message by attribute, so this is the real
    consumed shape rather than a stand-in for one.
    """

    topic: str
    value: bytes
    key: bytes | None = None
    headers: object | None = field(default=None)


# ---------------------------------------------------------------------------
# Real packaged gateway contract.
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def load_gateway_contract() -> dict[str, object]:
    """Parse the gateway contract shipped inside the pinned infra wheel."""

    raw = yaml.safe_load(INFRA_GATEWAY_CONTRACT_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("packaged gateway contract did not parse to a mapping")
    return raw


def _gateway_forwarder_block() -> dict[str, object]:
    config = load_gateway_contract()["config"]
    if not isinstance(config, dict):
        raise TypeError("gateway contract config block is not a mapping")
    block = config["gateway_forwarder"]
    if not isinstance(block, dict):
        raise TypeError("gateway contract gateway_forwarder block is not a mapping")
    return block


def gateway_contract_version() -> str:
    """The packaged contract's own ``contract_version``, rendered as semver.

    Read from the wheel rather than typed here so the S6 projection's version
    field is an observation of the shipped contract. A wheel bump that moves
    the contract version moves this string and the S6 golden fails — which is
    the drift signal, not noise.
    """

    raw = load_gateway_contract()["contract_version"]
    if not isinstance(raw, dict):
        raise TypeError("gateway contract_version block is not a mapping")
    return f"{raw['major']}.{raw['minor']}.{raw['patch']}"


def gateway_cloud_leg() -> dict[str, object]:
    """The contract's ``config.gateway_forwarder.cloud_leg`` block (S7 source)."""

    cloud_leg = _gateway_forwarder_block()["cloud_leg"]
    if not isinstance(cloud_leg, dict):
        raise TypeError("gateway contract cloud_leg block is not a mapping")
    return cloud_leg


@lru_cache(maxsize=1)
def gateway_mirror_topics() -> ModelGatewayMirrorTopics:
    """The contract-declared mirror topic sets, validated by the real model."""

    raw = _gateway_forwarder_block()["mirror_topics"]
    if not isinstance(raw, dict):
        raise TypeError("gateway contract mirror_topics block is not a mapping")
    inbound = raw["inbound"]
    outbound = raw["outbound"]
    if not isinstance(inbound, list) or not isinstance(outbound, list):
        raise TypeError("gateway contract mirror topic sets are not lists")
    return ModelGatewayMirrorTopics(
        inbound=tuple(str(topic) for topic in inbound),
        outbound=tuple(str(topic) for topic in outbound),
    )


def build_forwarder_config(*, dedupe_store_path: Path) -> ModelGatewayForwarderConfig:
    """Assemble the real forwarder config from the real packaged contract.

    Every wire-relevant value (mirror topics, the four cloud ``@ref`` pins, the
    SASL mechanism) is read out of the packaged contract rather than typed
    here, so the goldens exercise the deployed declaration. Only the tenant
    identity and the dedupe path are supplied by the test — they are per-deploy
    values with no contract-declared default.
    """

    cloud_leg = gateway_cloud_leg()
    cloud_bus = ModelGatewayCloudBusConfig(
        broker_provider_id=UUID(str(cloud_leg["broker_provider_id"])),
        cloud_broker_ref=str(cloud_leg["cloud_broker_ref"]),
        cloud_auth_ref=str(cloud_leg["cloud_auth_ref"]),
        acl_provisioner_ref=str(cloud_leg["acl_provisioner_ref"]),
        msk_region_ref=str(cloud_leg["msk_region_ref"]),
        security_protocol="SASL_SSL",
        sasl_mechanism="AWS_MSK_IAM",
    )
    return ModelGatewayForwarderConfig(
        tenant_identity=ModelGatewayTenantIdentity(
            tenant_id=GATEWAY_TENANT_ID,
            tenant_slug=GATEWAY_TENANT_SLUG,
            principal_id=GATEWAY_PRINCIPAL_ID,
        ),
        cloud_bus=cloud_bus,
        local_transport_flavor="containerized",
        mirror_topics=gateway_mirror_topics(),
        dedupe_store_path=dedupe_store_path,
    )


@dataclass(frozen=True)
class NodeEventBus:
    """A node contract's declared topic routing, read from the real file."""

    node_name: str
    subscribe_topics: tuple[str, ...]
    publish_topics: tuple[str, ...]


@cache
def omnimarket_node_event_bus(node_name: str) -> NodeEventBus:
    """Load an omnimarket node's contract-declared ``event_bus`` topic sets.

    Read from the node's real ``contract.yaml`` in ``src/``, never restated
    here. The contract IS the local dispatch routing declaration: a topic
    reaching the right node depends on this exact string set, so comparing the
    producer's wire topic against it is the routing leg of the seam, not a
    proxy for it.
    """

    path = REPO_ROOT / "src" / "omnimarket" / "nodes" / node_name / "contract.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError(f"node contract did not parse to a mapping: {path}")
    event_bus = raw["event_bus"]
    if not isinstance(event_bus, dict):
        raise TypeError(f"node contract has no event_bus mapping: {path}")
    subscribe = event_bus.get("subscribe_topics") or []
    publish = event_bus.get("publish_topics") or []
    if not isinstance(subscribe, list) or not isinstance(publish, list):
        raise TypeError(f"node contract topic sets are not lists: {path}")
    return NodeEventBus(
        node_name=node_name,
        subscribe_topics=tuple(str(topic) for topic in subscribe),
        publish_topics=tuple(str(topic) for topic in publish),
    )


# ---------------------------------------------------------------------------
# Envelope producers.
# ---------------------------------------------------------------------------


def cloud_hand_rolled_envelope_json(
    *,
    envelope_id: UUID,
    correlation_id: UUID,
    event_type: str,
    payload: dict[str, object],
    source_tenant_id: str,
    source_tenant_principal_id: str,
    extra_tags: dict[str, str] | None = None,
) -> bytes:
    """The cloud MSK producer's genuine hand-rolled JSON envelope (S4 producer).

    Built as a raw dict literal and ``json.dumps``-ed on purpose. The whole
    point of S4 is that the cloud side does NOT construct a
    ``ModelEventEnvelope`` — it hand-rolls the body, and only the local side
    parses it back into the typed model. Serializing the typed model here
    instead would make the golden vacuously green by construction: it would
    prove pydantic round-trips itself, not that the two independently written
    sides agree.

    Field set mirrors the registry's recorded producer shape exactly:
    ``envelope_id``, ``correlation_id``, ``source_tool``,
    ``metadata{headers,tags}``, ``event_type``, ``priority``, ``retry_count``,
    ``onex_version{major,minor,patch}``, ``envelope_version{...}``.
    """

    tags: dict[str, str] = {
        "source_tenant_id": source_tenant_id,
        "source_tenant_principal_id": source_tenant_principal_id,
    }
    if extra_tags:
        tags.update(extra_tags)

    body: dict[str, object] = {
        "envelope_id": str(envelope_id),
        "correlation_id": str(correlation_id),
        "source_tool": "onex-api",
        "metadata": {"headers": {}, "tags": tags},
        "event_type": event_type,
        "priority": 5,
        "retry_count": 0,
        "onex_version": {"major": 1, "minor": 0, "patch": 0},
        "envelope_version": {"major": 2, "minor": 1, "patch": 0},
        "payload": payload,
    }
    return json.dumps(body).encode("utf-8")


def local_typed_envelope(
    *,
    envelope_id: UUID,
    correlation_id: UUID,
    event_type: str,
    payload: dict[str, object],
    tags: dict[str, str] | None = None,
) -> ModelEventEnvelope[dict[str, object]]:
    """A local-runtime-produced typed envelope (the S2 outbound producer side).

    The local runtime genuinely builds the typed model, unlike the cloud
    publisher — so this side is constructed typed, and the asymmetry with
    :func:`cloud_hand_rolled_envelope_json` is the seam, not an inconsistency.
    """

    return ModelEventEnvelope[dict[str, object]](
        envelope_id=envelope_id,
        correlation_id=correlation_id,
        source_tool="omnibase-infra-local-runtime",
        event_type=event_type,
        payload=payload,
        metadata=ModelEnvelopeMetadata(tags=dict(tags or {})),
    )


# ---------------------------------------------------------------------------
# Registry access + the real three-leg matcher.
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _registry_document() -> dict[str, object]:
    manifest = load_slice_manifest()
    path = manifest.registry.resolved_path
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError(f"seams registry did not parse to a mapping: {path}")
    return raw


def registry_header() -> dict[str, object]:
    """The registry's provenance + summary header."""

    document = _registry_document()
    generated_from = document["generated_from"]
    summary = document["summary"]
    if not isinstance(generated_from, dict) or not isinstance(summary, dict):
        raise TypeError("seams registry header blocks are not mappings")
    return {
        "schema_version": document["schema_version"],
        "generated_from": generated_from,
        "summary": summary,
    }


@lru_cache(maxsize=1)
def _registry_edges() -> dict[str, dict[str, object]]:
    edges = _registry_document()["edges"]
    if not isinstance(edges, list):
        raise TypeError("seams registry edges block is not a list")
    resolved: dict[str, dict[str, object]] = {}
    for edge in edges:
        if not isinstance(edge, dict):
            raise TypeError("seams registry edge row is not a mapping")
        resolved[str(edge["edge_id"])] = edge
    return resolved


def registry_edge(edge_id: str) -> dict[str, object]:
    """One row of the registry of record."""

    edges = _registry_edges()
    if edge_id not in edges:
        raise KeyError(f"edge_id absent from seams.v1.yaml: {edge_id}")
    return edges[edge_id]


def registry_edge_ids() -> frozenset[str]:
    return frozenset(_registry_edges())


def registry_classification(edge_id: str) -> str:
    """The classification string recorded for an edge, unmapped.

    Exposed separately from :func:`assert_registry_classification` so a golden
    that has MEASURED the registry to be stale for its edge can pin that
    divergence explicitly, instead of either failing or quietly constructing
    projections that reproduce a classification the live code no longer has.
    """

    return str(registry_edge(edge_id)["classification"])


def _projection(
    *,
    edge_id: str,
    role: EnumSeamProjectionRole,
    topic: str,
    envelope_model: str,
    envelope_version: str,
    key_fields: tuple[tuple[str, str], ...],
    delivery_semantics: EnumSeamDeliverySemantics,
) -> ModelSeamProjection:
    return ModelSeamProjection(
        edge_id=edge_id,
        role=role,
        topic=topic,
        envelope_model=envelope_model,
        envelope_version=envelope_version,
        key_fields=tuple(
            ModelSeamProjectionField(name=name, field_type=field_type)
            for name, field_type in key_fields
        ),
        delivery_semantics=delivery_semantics,
    )


def producer_projection(
    *,
    edge_id: str,
    topic: str,
    envelope_model: str = SEAM_ENVELOPE_MODEL,
    envelope_version: str = SEAM_ENVELOPE_VERSION,
    key_fields: tuple[tuple[str, str], ...] = (),
    delivery_semantics: EnumSeamDeliverySemantics = (
        EnumSeamDeliverySemantics.AT_LEAST_ONCE
    ),
) -> ModelSeamProjection:
    """``seam-projection/v1`` for the producing side of an edge."""

    return _projection(
        edge_id=edge_id,
        role=EnumSeamProjectionRole.PRODUCER,
        topic=topic,
        envelope_model=envelope_model,
        envelope_version=envelope_version,
        key_fields=key_fields,
        delivery_semantics=delivery_semantics,
    )


def consumer_projection(
    *,
    edge_id: str,
    topic: str,
    envelope_model: str = SEAM_ENVELOPE_MODEL,
    envelope_version: str = SEAM_ENVELOPE_VERSION,
    key_fields: tuple[tuple[str, str], ...] = (),
    delivery_semantics: EnumSeamDeliverySemantics = (
        EnumSeamDeliverySemantics.AT_LEAST_ONCE
    ),
) -> ModelSeamProjection:
    """``seam-projection/v1`` for the consuming side of an edge."""

    return _projection(
        edge_id=edge_id,
        role=EnumSeamProjectionRole.CONSUMER,
        topic=topic,
        envelope_model=envelope_model,
        envelope_version=envelope_version,
        key_fields=key_fields,
        delivery_semantics=delivery_semantics,
    )


# ---------------------------------------------------------------------------
# Observation: building a projection from an artifact the test did not author.
# ---------------------------------------------------------------------------

#: Recorded as a field's type when the name the registry says crosses the wire
#: is simply not present in the artifact that crossed. Distinct from ``None``:
#: a model can default a missing field to ``None`` and look healthy, so the
#: presence check runs against the raw wire body, not the parsed object.
ABSENT_FROM_WIRE: Final[str] = "<absent-from-wire>"

#: Recorded as ``envelope_version`` for models that declare no wire version.
#: Naming the absence is honest; inventing ``"1.0.0"`` is not.
UNVERSIONED_MODEL: Final[str] = "unversioned"


def model_identity(model_cls: type) -> str:
    """Fully-qualified dotted name, with pydantic generic parametrization dropped.

    ``ModelEventEnvelope[dict[str, object]]`` and ``ModelEventEnvelope`` are the
    same wire model; the parametrization is a Python-side detail that must not
    read as an envelope-model mismatch.
    """

    metadata = getattr(model_cls, "__pydantic_generic_metadata__", None)
    if isinstance(metadata, dict) and metadata.get("origin") is not None:
        model_cls = metadata["origin"]
    return f"{model_cls.__module__}.{model_cls.__qualname__}"


def observed_type_name(value: object) -> str:
    """The seam type name of an OBSERVED runtime value.

    Deliberately coarse and deterministic: the seam cares whether a UUID
    crossed where a UUID was declared, not which UUID. ``bool`` is checked
    before ``int`` because ``bool`` is an ``int`` subclass and a boolean
    crossing where an integer was declared is a real seam difference.
    """

    if value is None:
        return "NoneType"
    if isinstance(value, UUID):
        return "UUID"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, Mapping):
        return "dict[str, object]"
    if isinstance(value, Sequence):
        return "list"
    return type(value).__name__


def annotated_type_name(annotation: object) -> str:
    """The seam type name of a DECLARED model annotation.

    The class-level mirror of :func:`observed_type_name`, used when the artifact
    being observed is a model class the contract names rather than an instance
    that crossed. ``X | None`` collapses to ``X``: optionality is a producer
    liveness question the correlation assertions cover, not a type identity.
    """

    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin in (types.UnionType, typing.Union):
        present = [arg for arg in args if arg is not type(None)]
        if len(present) == 1:
            return annotated_type_name(present[0])
        return " | ".join(annotated_type_name(arg) for arg in present)
    if origin is dict:
        return "dict[str, object]"
    if origin in (list, tuple, set, frozenset):
        return str(origin.__name__)
    name = getattr(annotation, "__name__", None)
    return str(name) if name is not None else str(annotation)


def wire_body(raw: bytes) -> dict[str, object]:
    """Parse captured wire bytes into the raw mapping that actually crossed."""

    body = json.loads(raw)
    if not isinstance(body, dict):
        raise TypeError("captured wire bytes did not parse to a JSON object")
    return body


def _observed_version(instance: object) -> str:
    """Read a wire version off a real instance, or name its absence."""

    envelope_version = getattr(instance, "envelope_version", None)
    if envelope_version is not None:
        return str(envelope_version)
    schema_version = getattr(instance, "schema_version", None)
    if schema_version is not None:
        return str(schema_version)
    return UNVERSIONED_MODEL


def _reject_aliased_observation(
    *,
    edge_id: str,
    declared_producer: ModelSeamProjection | None,
    declared_consumer: ModelSeamProjection | None,
    observed_producer: ModelSeamProjection | None,
    observed_consumer: ModelSeamProjection | None,
) -> None:
    """Fail closed when an observed side is literally a declared side.

    Object identity, not value equality: a genuinely observed projection is
    ALLOWED to compare equal to the declared one — that equality is precisely
    what earns ``REGENERABLE``. What is never allowed is the same object
    arriving on both sides, because then no observation happened at all.
    """

    declared = {
        "declared_producer": declared_producer,
        "declared_consumer": declared_consumer,
    }
    observed = {
        "observed_producer": observed_producer,
        "observed_consumer": observed_consumer,
    }
    for observed_name, observed_value in observed.items():
        if observed_value is None:
            continue
        for declared_name, declared_value in declared.items():
            if declared_value is observed_value:
                raise AssertionError(
                    f"{edge_id}: {observed_name} is the same object as "
                    f"{declared_name}; that makes the observed-vs-declared leg "
                    f"a self-comparison and REGENERABLE unconditional. Build "
                    f"the observed projection from what the golden actually "
                    f"drove (observed_projection_from_instance / "
                    f"_from_mapping / _from_model_class), or pass None and "
                    f"classify the edge SHAPE_ONLY."
                )


def observed_projection_from_instance(
    *,
    edge_id: str,
    role: EnumSeamProjectionRole,
    topic: str,
    instance: object,
    field_names: tuple[str, ...],
    body: Mapping[str, object] | None = None,
    delivery_semantics: EnumSeamDeliverySemantics = (
        EnumSeamDeliverySemantics.AT_LEAST_ONCE
    ),
) -> ModelSeamProjection:
    """Project a model instance that real code produced or really parsed.

    ``topic`` must be the topic the artifact was genuinely published to or read
    from, sourced independently of the declared projection — the whole point is
    that a producer publishing to a renamed topic diverges here.

    ``body`` is the raw wire mapping when one exists. Presence is decided by it
    rather than by the parsed object, because a model that defaults a dropped
    field still yields an attribute: only the raw body can distinguish "the
    producer sent null" from "the producer stopped sending it".
    """

    key_fields: list[tuple[str, str]] = []
    for name in field_names:
        if body is not None and name not in body:
            key_fields.append((name, ABSENT_FROM_WIRE))
            continue
        key_fields.append((name, observed_type_name(getattr(instance, name, None))))

    return _projection(
        edge_id=edge_id,
        role=role,
        topic=topic,
        envelope_model=model_identity(type(instance)),
        envelope_version=_observed_version(instance),
        key_fields=tuple(key_fields),
        delivery_semantics=delivery_semantics,
    )


def observed_projection_from_mapping(
    *,
    edge_id: str,
    role: EnumSeamProjectionRole,
    topic: str,
    mapping: Mapping[str, object],
    field_names: tuple[str, ...],
    envelope_model: str,
    envelope_version: str = UNVERSIONED_MODEL,
    delivery_semantics: EnumSeamDeliverySemantics = (
        EnumSeamDeliverySemantics.AT_LEAST_ONCE
    ),
) -> ModelSeamProjection:
    """Project a raw mapping that real code produced (a payload, a tag block).

    Used where the crossing artifact is a bare ``dict`` rather than a model —
    the gateway's stamped payload (S11) and the tenant-attribution tag block
    (S5). ``envelope_model`` is supplied by the caller because a bare mapping
    has no class identity of its own; callers pass the identity of a model they
    have DEMONSTRATED the mapping validates against, never an unproven claim.
    """

    key_fields = tuple(
        (
            name,
            observed_type_name(mapping[name]) if name in mapping else ABSENT_FROM_WIRE,
        )
        for name in field_names
    )
    return _projection(
        edge_id=edge_id,
        role=role,
        topic=topic,
        envelope_model=envelope_model,
        envelope_version=envelope_version,
        key_fields=key_fields,
        delivery_semantics=delivery_semantics,
    )


def observed_projection_from_model_class(
    *,
    edge_id: str,
    role: EnumSeamProjectionRole,
    topic: str,
    model_cls: type[object],
    field_names: tuple[str, ...],
    envelope_version: str = UNVERSIONED_MODEL,
    delivery_semantics: EnumSeamDeliverySemantics = (
        EnumSeamDeliverySemantics.AT_LEAST_ONCE
    ),
) -> ModelSeamProjection:
    """Project a model class a real committed contract names.

    The S6 producer is a declaration, not a message: the packaged
    ``contract.yaml`` names ``input_model``, and what that class actually
    declares is the producer's observable shape. Field types come from the
    class annotations via :func:`annotated_type_name`; a field the contract's
    model no longer declares reads as :data:`ABSENT_FROM_WIRE`.
    """

    model_fields = getattr(model_cls, "model_fields", {})
    key_fields: list[tuple[str, str]] = []
    for name in field_names:
        info = model_fields.get(name)
        if info is None:
            key_fields.append((name, ABSENT_FROM_WIRE))
            continue
        key_fields.append((name, annotated_type_name(info.annotation)))

    return _projection(
        edge_id=edge_id,
        role=role,
        topic=topic,
        envelope_model=model_identity(model_cls),
        envelope_version=envelope_version,
        key_fields=tuple(key_fields),
        delivery_semantics=delivery_semantics,
    )


def run_registry_match(
    *,
    edge_id: str,
    declared_producer: ModelSeamProjection | None,
    declared_consumer: ModelSeamProjection | None,
    observed_producer: ModelSeamProjection | None = None,
    observed_consumer: ModelSeamProjection | None = None,
) -> ModelSeamMatchVerdict:
    """Drive the REAL ``HandlerSeamMatch`` — the registry leg of every golden.

    ``observed_*`` are the projections derived from what the golden actually
    drove through the live code, so supplying both is what earns
    ``REGENERABLE`` from the shipped classifier: legs 2 and 3 compare observed
    against declared, and the handler refuses to call a leg-1-only shape match
    regenerable. That refusal is exactly the plan's "a shape comparison is
    insufficient" bar, enforced by the production classifier rather than
    restated in an assertion here.

    **Tautology guard.** Handing the same projection object to a ``declared_*``
    and an ``observed_*`` parameter reduces the corresponding leg to ``x == x``,
    which passes unconditionally and makes ``REGENERABLE`` insensitive to
    everything the golden drove. That is rejected here rather than merely
    discouraged, because a comment cannot stop the next edit from reintroducing
    it. Observed projections must be BUILT from a driven artifact — see the
    ``observed_projection_from_*`` constructors above.

    Any ``projection_pinned_hash`` recorded on the row is threaded through to
    the stale-proof detector. Every row currently carries ``null`` there (the
    registry pins only a prose ``source_record_pinned_hash``, a different hash
    namespace), so no stale check runs today — but the moment the generator
    starts pinning projections, these goldens begin enforcing staleness with
    no edit here.
    """

    _reject_aliased_observation(
        edge_id=edge_id,
        declared_producer=declared_producer,
        declared_consumer=declared_consumer,
        observed_producer=observed_producer,
        observed_consumer=observed_consumer,
    )

    row = registry_edge(edge_id)
    pinned = row.get("projection_pinned_hash")
    return HandlerSeamMatch().handle(
        ModelSeamMatchRequest(
            edge_id=edge_id,
            declared_producer=declared_producer,
            declared_consumer=declared_consumer,
            observed_producer=observed_producer,
            observed_consumer=observed_consumer,
            pinned_hash=str(pinned) if pinned is not None else None,
        )
    )


# The registry records "leg 1 matched but nothing executable drove it" as
# MATCHED_UNTESTED. The live classifier only emits UNMATCHED / MISMATCH /
# MATCHED, so MATCHED_UNTESTED collapses onto MATCHED for the leg-1 comparison.
# These goldens are precisely the executable proof that retires the _UNTESTED
# qualifier; re-pinning the registry row is a generator run, tracked as
# evidence on the OCC receipt rather than by hand-editing a generated file.
_LEG1_EQUIVALENT: Final[dict[str, str]] = {"MATCHED_UNTESTED": "MATCHED"}


def assert_registry_classification(
    edge_id: str, verdict: ModelSeamMatchVerdict
) -> None:
    """Bind a golden's live verdict to the registry's recorded classification.

    This is the guard against the two failure modes that make a seam registry
    worthless: a registry row that says MATCHED while the live seam mismatches,
    and a golden that quietly proves something other than what the registry
    claims. Either drift fails here.
    """

    recorded = str(registry_edge(edge_id)["classification"])
    expected = _LEG1_EQUIVALENT.get(recorded, recorded)
    if verdict.verdict.value != expected:
        raise AssertionError(
            f"{edge_id}: live seam match returned {verdict.verdict.value} but "
            f"seams.v1.yaml records {recorded}; the registry and the real seam "
            f"have drifted apart"
        )


def assert_regenerable(edge_id: str, verdict: ModelSeamMatchVerdict) -> None:
    """Assert a genuinely two-sided observation earned ``REGENERABLE``.

    Both observed legs must be explicitly green — not merely "not red" — and
    the frozen slice must agree the edge is observable on both sides. Binding
    to ``slice_manifest.yaml`` here is what stops a golden from claiming an
    observation the manifest says is impossible.
    """

    edge = load_slice_manifest().by_id(edge_id)
    if edge.observation_class is not EnumSeamObservationClass.REGENERABLE:
        raise AssertionError(
            f"{edge_id}: golden asserts REGENERABLE but slice_manifest.yaml "
            f"records observation_class={edge.observation_class}; the "
            f"manifest and the golden must agree on what is observable"
        )
    if verdict.leg2_observed_producer_vs_declared.passed is not True:
        raise AssertionError(
            f"{edge_id}: leg 2 (observed producer vs declared) is "
            f"{verdict.leg2_observed_producer_vs_declared.passed!r}, mismatching "
            f"field {verdict.leg2_observed_producer_vs_declared.mismatching_field_path!r}"
        )
    if verdict.leg3_observed_consumer_vs_declared.passed is not True:
        raise AssertionError(
            f"{edge_id}: leg 3 (observed consumer vs declared) is "
            f"{verdict.leg3_observed_consumer_vs_declared.passed!r}, mismatching "
            f"field {verdict.leg3_observed_consumer_vs_declared.mismatching_field_path!r}"
        )
    if verdict.regenerability.value != "REGENERABLE":
        raise AssertionError(
            f"{edge_id}: both observed legs are green but the classifier "
            f"returned {verdict.regenerability.value}"
        )


def assert_shape_only(
    edge_id: str,
    verdict: ModelSeamMatchVerdict,
    *,
    producer_observed: bool,
    consumer_observed: bool,
) -> None:
    """Assert an edge is honestly SHAPE_ONLY, and for the recorded reason.

    ``SHAPE_ONLY`` is a real result, not a failure — but it must be the result
    of an unobservable side, not of a silently broken observation. The caller
    states which sides it could observe, and this checks the classifier's legs
    agree: an unobserved side must be ``None`` (not evaluated) and an observed
    side must be green. A leg that went RED is a seam defect and fails here
    instead of being absorbed into "well, it's shape-only anyway".
    """

    edge = load_slice_manifest().by_id(edge_id)
    if edge.observation_class is not EnumSeamObservationClass.SHAPE_ONLY:
        raise AssertionError(
            f"{edge_id}: golden asserts SHAPE_ONLY but slice_manifest.yaml "
            f"records observation_class={edge.observation_class}"
        )
    expectations = (
        (
            "leg 2 (producer)",
            producer_observed,
            verdict.leg2_observed_producer_vs_declared,
        ),
        (
            "leg 3 (consumer)",
            consumer_observed,
            verdict.leg3_observed_consumer_vs_declared,
        ),
    )
    for label, was_observed, leg in expectations:
        if was_observed and leg.passed is not True:
            raise AssertionError(
                f"{edge_id}: {label} was observed but did not pass "
                f"({leg.passed!r}, field {leg.mismatching_field_path!r}) — the "
                f"observable half of this seam has drifted"
            )
        if not was_observed and leg.passed is not None:
            raise AssertionError(
                f"{edge_id}: {label} is recorded as unobservable but the "
                f"classifier evaluated it ({leg.passed!r}); the golden is "
                f"supplying an observation the manifest denies"
            )
    if verdict.regenerability.value != "SHAPE_ONLY":
        raise AssertionError(
            f"{edge_id}: expected SHAPE_ONLY, classifier returned "
            f"{verdict.regenerability.value}"
        )


def assert_correlation_preserved(
    *, edge_id: str, emitted: UUID, observed: UUID | None
) -> None:
    """The correlation-preservation bar this ticket sets above shape matching.

    ``observed`` is typed optional because ``ModelEventEnvelope.correlation_id``
    is optional — a hop that DROPS the correlation id produces ``None`` here,
    and that must fail loudly rather than compare equal to nothing.
    """

    if observed is None:
        raise AssertionError(
            f"{edge_id}: correlation_id was dropped at the consumer "
            f"(producer emitted {emitted}); the seam is shape-compatible but "
            f"does not preserve correlation"
        )
    if observed != emitted:
        raise AssertionError(
            f"{edge_id}: correlation_id was rewritten across the seam — "
            f"producer emitted {emitted}, consumer observed {observed}"
        )


def repo_path(relative: str) -> Path:
    """Resolve a repo-relative path without hardcoding an absolute prefix."""

    return REPO_ROOT / relative
