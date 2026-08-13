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
"""

from __future__ import annotations

import json
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
from tests.seam_goldens.manifest import REPO_ROOT, load_slice_manifest

__all__ = [
    "GATEWAY_PRINCIPAL_ID",
    "GATEWAY_TENANT_ID",
    "GATEWAY_TENANT_SLUG",
    "INFRA_GATEWAY_CONTRACT_PATH",
    "SEAM_ENVELOPE_MODEL",
    "SEAM_ENVELOPE_VERSION",
    "BusMessage",
    "RecordingPublisher",
    "assert_correlation_preserved",
    "assert_registry_classification",
    "build_forwarder_config",
    "cloud_hand_rolled_envelope_json",
    "consumer_projection",
    "gateway_cloud_leg",
    "gateway_mirror_topics",
    "load_gateway_contract",
    "local_typed_envelope",
    "omnimarket_node_event_bus",
    "producer_projection",
    "registry_edge",
    "registry_header",
    "run_registry_match",
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

    Any ``projection_pinned_hash`` recorded on the row is threaded through to
    the stale-proof detector. Every row currently carries ``null`` there (the
    registry pins only a prose ``source_record_pinned_hash``, a different hash
    namespace), so no stale check runs today — but the moment the generator
    starts pinning projections, these goldens begin enforcing staleness with
    no edit here.
    """

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
