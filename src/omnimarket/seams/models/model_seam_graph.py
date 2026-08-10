# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""``seam-graph/v1`` output models for node_seam_graph_compute (OMN-15763).

**Schema extension (post-merge fix-forward pass, 2026-08-08 addendum
reconciliation).** The addendum flagged, and adversarial verify of the
merged PR confirmed unresolved, that this schema must carry per edge: "topic
name including the tenant-prefix rule, envelope model + version, key
fields/types, producer/consumer contract paths, and the FSM state
transitions the edge participates in" — because OMN-15756's registry
consumers and OMN-15767's DAG-walk consumer both depend on the expanded
shape being right the first time. ``ModelSeamGraphEdgeDeclaration`` now
carries ``key_fields``, ``delivery_semantics``, ``producer_contract_path``,
``consumer_contract_path``, and ``fsm_state_transitions`` — all optional
(default empty/``None``/``UNKNOWN``) since a producing ``contract.yaml``'s
``seams:`` block entry may not declare every one of them yet, and the
counterpart contract path is filled by post-extraction correlation (below),
not by the declaring side alone.

Two extraction classes, kept structurally distinct so a reviewer can see
which is which:

* ``ModelSeamGraphEdgeDeclaration`` — a declared seam edge read from a
  producing ``contract.yaml``'s ``seams:`` block (proposal step 1). This is
  the authoritative, typed declaration.
* ``ModelSeamGraphCodeObservation`` — a raw code-level observation (Kafka
  producer/consumer topic literal, ``os.environ`` read, ``@ref`` pin) found
  by scanning source files. These are evidence, not declarations — they feed
  ``node_seam_match_compute``'s "observed" legs once correlated to an edge.

Both are covered by the same per-source sha256 manifest (mirroring
``node_contract_graph_ir_compute``) so the whole graph is provably
reproducible from a pinned tree (AC7): re-running against the same source
bytes always yields the same edges, the same observations, and the same
manifest, because both are derived by sorting deterministically and hashing
file bytes rather than relying on filesystem iteration order.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.seams.models.model_seam_projection import (
    EnumSeamDeliverySemantics,
    ModelSeamProjectionField,
)

__all__ = [
    "EnumSeamGraphObservationKind",
    "ModelSeamGraphCodeObservation",
    "ModelSeamGraphEdgeDeclaration",
    "ModelSeamGraphSourceHashEntry",
    "ModelSeamGraphV1",
]


class EnumSeamGraphObservationKind(StrEnum):
    """Which code-level extractor produced this observation.

    ``*_UNRESOLVED`` (OMN-15779) — the call site matched a producer/consumer
    receiver+method shape (a real seam), but the topic argument could not be
    statically resolved to a literal (a dynamic f-string, an unmodeled
    function call, an ``os.environ``-derived value, etc.). Emitted so a
    genuinely-dynamic call site is disclosed as an explicit, typed
    observation rather than silently dropped — ``value`` carries a
    best-effort ``ast.unparse()`` of the unresolved argument expression, not
    a fabricated topic string.

    ``REF_PIN_RESOLVED`` (OMN-15779) — a ``@ref:<file>.yaml#<dotted.key>``
    pin whose target file+key was actually read and resolved to a literal
    string value, in addition to the raw ``REF_PIN`` observation (which
    always fires regardless of whether resolution succeeds)."""

    PRODUCER_SEND = "producer_send"
    CONSUMER_SUBSCRIBE = "consumer_subscribe"
    ENV_READ = "env_read"
    REF_PIN = "ref_pin"
    PRODUCER_SEND_UNRESOLVED = "producer_send_unresolved"
    CONSUMER_SUBSCRIBE_UNRESOLVED = "consumer_subscribe_unresolved"
    REF_PIN_RESOLVED = "ref_pin_resolved"


class ModelSeamGraphEdgeDeclaration(BaseModel):
    """One seam edge declared in a contract.yaml — either a ``seams:`` block
    entry (proposal-step-1 schema, not yet adopted by any real contract) or
    an ``event_bus.publish_topics`` / ``event_bus.subscribe_topics`` entry
    (the schema 435+ real contracts across omnibase_infra/omnimarket/
    omnibase_core actually carry today — OMN-15763 AC1 fix-forward).

    ``key_fields`` / ``delivery_semantics`` mirror ``ModelSeamProjection``'s
    wire-crossing fields (same types, reused rather than duplicated) so a
    declaration can be lifted directly into a projection once field-level
    extraction backs it. ``producer_contract_path`` /
    ``consumer_contract_path`` are filled by ``extract_seam_graph``'s
    post-extraction correlation pass — the declaring side's own path is
    known immediately, the counterpart's only once a matching-edge_id
    declaration with the opposite role is found somewhere in the same scan.
    ``fsm_state_transitions`` names the FSM states this edge participates in
    (OMN-15767's DAG-walk consumer), read from an optional
    ``fsm_state_transitions:`` list on the seams: entry.

    ``envelope_model`` / ``envelope_version`` are optional (``None``, not a
    fabricated placeholder) because the real ``event_bus.publish_topics`` /
    ``subscribe_topics`` schema does not carry per-topic envelope type
    information — only the hand-authored ``seams:`` schema does. A ``None``
    here means "not declared by this source," never "unknown but assumed."
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    edge_id: str = Field(min_length=1)
    seam: str = Field(min_length=1)
    role: str = Field(min_length=1)
    source_contract_path: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    envelope_model: str | None = Field(default=None, min_length=1)
    envelope_version: str | None = Field(default=None, min_length=1)
    key_fields: tuple[ModelSeamProjectionField, ...] = Field(default_factory=tuple)
    delivery_semantics: EnumSeamDeliverySemantics = EnumSeamDeliverySemantics.UNKNOWN
    producer_contract_path: str | None = None
    consumer_contract_path: str | None = None
    fsm_state_transitions: tuple[str, ...] = Field(default_factory=tuple)


class ModelSeamGraphCodeObservation(BaseModel):
    """One raw code-level observation from scanning a source file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_path: str = Field(min_length=1)
    kind: EnumSeamGraphObservationKind
    value: str = Field(min_length=1)
    line_number: int = Field(ge=1)


class ModelSeamGraphSourceHashEntry(BaseModel):
    """Per-source sha256 manifest entry (determinism proof, AC7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_path: str = Field(min_length=1)
    source_sha256: str = Field(min_length=64, max_length=64)


class ModelSeamGraphV1(BaseModel):
    """The full ``seam-graph/v1`` output: declared edges + code observations
    + the per-source sha256 manifest that proves the graph is reproducible.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["seam-graph/v1"] = "seam-graph/v1"
    discovery_roots: tuple[str, ...] = Field(default_factory=tuple)
    edges: tuple[ModelSeamGraphEdgeDeclaration, ...] = Field(default_factory=tuple)
    code_observations: tuple[ModelSeamGraphCodeObservation, ...] = Field(
        default_factory=tuple
    )
    source_manifest: tuple[ModelSeamGraphSourceHashEntry, ...] = Field(
        default_factory=tuple
    )
