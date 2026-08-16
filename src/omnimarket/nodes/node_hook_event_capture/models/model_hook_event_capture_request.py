# SPDX-License-Identifier: MIT
"""Typed input models for node_hook_event_capture (OMN-16090).

These models are the CONSUMER half of the gateway seam. The gateway's
``workflow-contracts.yaml`` entry for ``hook-event-capture`` and the wire
passthrough spec in ``models/model_workflow_envelope.py`` are the producer
half; the two must be matched field-for-field, and the fields below carry the
reason each one is shaped the way it is so a later edit on either side is
forced to confront the other.

The gateway's own validator is a hand-rolled JSON-Schema subset with no
``enum``, no ``oneOf`` and no cross-field rules. It is a coarse, fail-closed
structural filter, not a business-rule engine. The real invariants live here.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# sha256 hex digest, lowercase. Both the per-event dedupe key and the batch
# key use this shape.
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

# The immutable tenant principal derived from the tenant UUID
# (onex-api tenant_identity.derive_principal_id). NOT the slug: the slug is
# attribution-only on this wire and is overwritten by the forwarder path.
TENANT_PRINCIPAL = re.compile(r"^t-[0-9a-f]{32}$")

# A producer event type: either a canonical ONEX topic-shaped name
# ("onex.evt.omniclaude.skill-started.v1") or a bare dotted name
# ("artifact.captured"). Deliberately NOT an allowlist of the four measured
# families -- a new hook family must not need a release of this node to be
# capturable.
EVENT_TYPE = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$")

MAX_EVENTS_PER_BATCH = 250
MAX_PAYLOAD_JSON_CHARS = 32768


class ModelCapturedHookEvent(BaseModel):
    """One captured event inside a batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: str = Field(min_length=3, max_length=200)
    event_sha: str
    occurred_at: str = Field(min_length=20, max_length=40)
    payload_json: str = Field(min_length=2, max_length=MAX_PAYLOAD_JSON_CHARS)

    # Correlation only, and all genuinely optional. Two of the four measured
    # event families carry no event_id whatsoever; requiring it here would
    # reject 61% of the real corpus at the consumer after the gateway already
    # accepted it, which is the worst possible place to discover a seam
    # mismatch.
    event_id: str | None = Field(default=None, max_length=64)
    correlation_id: str | None = Field(default=None, max_length=64)
    run_id: str | None = Field(default=None, max_length=64)
    spooled_at: str | None = Field(default=None, max_length=40)
    # Why the producer's local emit failed. Retained because a capture path
    # that discards it destroys the only evidence of WHY the events were
    # stranded in the first place.
    spool_reason: str | None = Field(default=None, max_length=512)

    @field_validator("event_type")
    @classmethod
    def _event_type_shape(cls, value: str) -> str:
        if not EVENT_TYPE.fullmatch(value):
            raise ValueError(
                f"event_type {value!r} is not a dotted lowercase producer type"
            )
        return value

    @field_validator("event_sha")
    @classmethod
    def _sha_shape(cls, value: str) -> str:
        if not SHA256_HEX.fullmatch(value):
            raise ValueError(
                "event_sha must be a lowercase sha256 hex digest — it is the "
                "durable idempotency key, so a malformed one would silently "
                "create a duplicate row rather than dedupe against the original"
            )
        return value

    @field_validator("payload_json")
    @classmethod
    def _payload_is_json_object(cls, value: str) -> str:
        """The body is opaque to this node, but it must be a JSON OBJECT.

        Opaque does not mean unparsed. The column is JSONB, so a non-JSON
        string fails at the database with a driver-level error that names
        nothing useful; and a bare scalar (``"3"``, ``"null"``) would store a
        JSONB scalar where every reader expects an object. Rejecting here
        turns both into an actionable message naming the event.
        """
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"payload_json is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(
                f"payload_json must decode to a JSON object, got {type(parsed).__name__}"
            )
        return value

    def payload(self) -> dict[str, Any]:
        """The decoded body. Safe after validation."""
        decoded: dict[str, Any] = json.loads(self.payload_json)
        return decoded


class ModelHookEventCaptureRequest(BaseModel):
    """A gateway-submitted batch of captured hook events.

    Field set is the gateway's passthrough spec (source, batch_sha, events)
    plus the fields the gateway injects on every workflow envelope
    (correlation_id, emitted_at, tenant_id) and the immutable principal it
    injects for tenant-keyed workflows (tenant_principal_id).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=3, max_length=64)
    batch_sha: str
    events: list[ModelCapturedHookEvent] = Field(
        min_length=1, max_length=MAX_EVENTS_PER_BATCH
    )

    # Gateway-injected on every envelope.
    correlation_id: str
    emitted_at: str

    # ATTRIBUTION ONLY. onex-api documents this explicitly: the forwarder path
    # overwrites it with its config-bound slug and the direct-lane consumer
    # falls back to ONEX_TENANT_ID when it is absent. It is NEVER the
    # authorization or storage key here.
    tenant_id: str | None = Field(default=None, max_length=128)

    # The real tenant key. Immutable, derived from the tenant UUID, never from
    # the slug. Required, because the dedupe key is (tenant, event_sha) and
    # keying tenant-isolated rows on a mutable overwritable slug would merge or
    # split two tenants' event history with nothing raised anywhere.
    tenant_principal_id: str

    @field_validator("batch_sha")
    @classmethod
    def _batch_sha_shape(cls, value: str) -> str:
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("batch_sha must be a lowercase sha256 hex digest")
        return value

    @field_validator("tenant_principal_id")
    @classmethod
    def _principal_shape(cls, value: str) -> str:
        if not TENANT_PRINCIPAL.fullmatch(value):
            raise ValueError(
                f"tenant_principal_id {value!r} is not an immutable tenant "
                "principal ('t-<32hex>'). The tenant slug is not accepted here: "
                "it is attribution-only on this wire and must never become a "
                "storage key."
            )
        return value
