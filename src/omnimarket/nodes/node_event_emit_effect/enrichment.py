# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Envelope enrichment for ``node_event_emit_effect`` (OMN-16018 / OMN-16020).

This module is the node-side reproduction of the legacy emit daemon's
publish-time envelope enrichment. OMN-16048 ruled REPLICATE: the enrichment
fields and the partition-key computation are **wire contract**, not daemon
implementation detail, so the node must reproduce them byte-for-byte rather
than narrow the parity spec.

Reference implementation (read it before changing anything here):
    ``omnimarket/nodes/node_emit_daemon/socket_server.py``
        ``EmitSocketServer._inject_metadata`` -- the five injected fields.
    ``omnimarket/nodes/node_emit_daemon/event_registry.py``
        ``transform_passthrough`` / ``transform_strip_prompt`` /
        ``transform_strip_body`` -- the per-fan-out-rule payload transforms.
        ``EventRegistry.get_partition_key`` -- the partition-key computation.

The three transform bodies and ``inject_metadata`` are deliberate verbatim
ports, not imports: ``node_event_emit_effect`` must never import
``omnimarket.nodes.node_emit_daemon.*`` because that entire Python surface is
deleted under R5 (OMN-15974). See ``spool/topic_resolver.py``'s module
docstring for the full rule. ``registries/topics.yaml`` (a data file) remains
the one shared surface, read by path.

Determinism seam
----------------
Two of the injected fields are generated rather than derived:

* ``emitted_at`` -- the publish-time UTC instant.
* ``correlation_id`` -- a fresh UUID, but ONLY when the payload does not
  already carry one.

The daemon generates both inline (``datetime.now(UTC)`` / ``uuid4()``), so
two independent runs of the same code can never produce the same bytes for
them. Both sides therefore take these as injected callables with exactly
those production defaults, so a parity harness can drive the two paths with
one shared clock/ID source and compare the *generation policy* (when a value
is preserved vs. minted, and how it is formatted) byte-for-byte instead of
waiving the fields. The seam changes nothing at runtime -- the defaults are
the daemon's own expressions.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from uuid import UUID, uuid4

from omnimarket.nodes.node_event_emit_effect.errors import (
    TopicScopedTransformError,
    UnknownTransformError,
)
from omnimarket.nodes.node_event_emit_effect.redaction import redact_capture

JsonDict = dict[str, object]

#: Emitted verbatim into every payload. Matches
#: ``EmitSocketServer._inject_metadata``'s literal.
SCHEMA_VERSION = "1.0.0"

#: Env var the daemon falls back to when the payload carries no ``session_id``.
SESSION_ID_ENV_VAR = "CLAUDE_CODE_SESSION_ID"

#: The fields ``inject_metadata`` may add. Declared here so
#: ``contract.yaml``'s ``envelope_enrichment`` block and this implementation
#: can be asserted equal by a contract-compliance test rather than drifting.
ENRICHMENT_FIELDS: tuple[str, ...] = (
    "correlation_id",
    "causation_id",
    "emitted_at",
    "session_id",
    "entity_id",
    "schema_version",
)

#: The subset ``inject_metadata`` adds to EVERY payload regardless of input.
#: ``session_id`` is only injected when the payload lacks it AND the env var is
#: set; ``entity_id`` only when a ``session_id`` is available to derive it from
#: -- so neither is unconditional, and asserting their presence would make a
#: test environment-dependent.
UNCONDITIONAL_ENRICHMENT_FIELDS: tuple[str, ...] = (
    "correlation_id",
    "causation_id",
    "emitted_at",
    "schema_version",
)


def default_clock() -> datetime:
    """Production clock: the daemon's ``datetime.now(UTC)``."""
    return datetime.now(UTC)


def default_correlation_id_factory() -> str:
    """Production ID source: the daemon's ``str(uuid4())``."""
    return str(uuid4())


def inject_metadata(
    payload: JsonDict,
    correlation_id: str | None = None,
    *,
    clock: Callable[[], datetime] = default_clock,
    correlation_id_factory: Callable[[], str] = default_correlation_id_factory,
    env: Mapping[str, str] | None = None,
) -> JsonDict:
    """Add the standard envelope metadata fields to ``payload``.

    Byte-for-byte port of ``EmitSocketServer._inject_metadata``. Every branch
    below mirrors a branch there; the ordering of the assignments is part of
    the contract because it fixes the JSON key order on the wire.

    Args:
        payload: The caller-supplied event payload. Never mutated.
        correlation_id: Fallback correlation ID used only when ``payload``
            carries none. The daemon sources this from the payload itself
            (making it redundant there); the node sources it from
            ``ModelEmitRequest.correlation_id``, which is a strict superset of
            the daemon's behavior -- when the payload has one, it still wins.
        clock: Injected UTC clock (see module docstring).
        correlation_id_factory: Injected ID source (see module docstring).
        env: Environment mapping for the ``session_id`` fallback. Defaults to
            ``os.environ``.

    Returns:
        A new dict: ``payload`` plus the injected fields.
    """
    environ: Mapping[str, str] = os.environ if env is None else env

    result: JsonDict = dict(payload)
    if "correlation_id" not in result or result["correlation_id"] is None:
        result["correlation_id"] = correlation_id or correlation_id_factory()
    if "causation_id" not in result:
        result["causation_id"] = None
    if "emitted_at" not in result:
        result["emitted_at"] = clock().isoformat()
    # session_id: preserve payload value; fall back to CLAUDE_CODE_SESSION_ID.
    # Clients are the canonical source -- the emitter is session-agnostic.
    if "session_id" not in result or not result["session_id"]:
        env_session_id = environ.get(SESSION_ID_ENV_VAR, "")
        if env_session_id:
            result["session_id"] = env_session_id
    # Derive entity_id from session_id when absent.
    if "entity_id" not in result:
        session_id = result.get("session_id")
        if isinstance(session_id, str) and session_id:
            try:
                UUID(session_id)
                result["entity_id"] = session_id
            except ValueError:
                h = hashlib.sha256(session_id.encode()).hexdigest()[:32]
                result["entity_id"] = str(UUID(h))
    result["schema_version"] = SCHEMA_VERSION
    return result


# ---------------------------------------------------------------------------
# Per-fan-out-rule payload transforms
# ---------------------------------------------------------------------------


def transform_passthrough(payload: JsonDict) -> JsonDict:
    """Passthrough transform -- returns payload unchanged."""
    return payload


def transform_strip_prompt(payload: JsonDict) -> JsonDict:
    """Strip full prompt content, keeping only preview and length.

    Suitable for observability topics where full prompt must not appear.
    """
    result: JsonDict = dict(payload)

    result.pop("prompt_b64", None)
    result.pop("prompt", None)

    preview = payload.get("prompt_preview", "")
    if not isinstance(preview, str):
        preview = str(preview) if preview is not None else ""
    result["prompt_preview"] = preview[:100]

    if "prompt_length" not in result:
        full_prompt = payload.get("prompt", "")
        if isinstance(full_prompt, str):
            result["prompt_length"] = len(full_prompt)

    return result


def transform_strip_body(payload: JsonDict) -> JsonDict:
    """Strip body field, replacing with length and preview."""
    result: JsonDict = dict(payload)
    body = payload.get("body", "")
    if not isinstance(body, str):
        body = str(body) if body is not None else ""
    result["body_length"] = len(body)
    result["body_preview"] = body[:200]
    result.pop("body", None)
    return result


#: Symbolic transform names as declared in ``registries/topics.yaml``'s
#: ``fan_out[].transform``. Mirrors ``event_registry.TRANSFORM_REGISTRY``.
TRANSFORM_REGISTRY: dict[str, Callable[[JsonDict], JsonDict]] = {
    "passthrough": transform_passthrough,
    "strip_prompt": transform_strip_prompt,
    "strip_body": transform_strip_body,
}

#: Transforms whose posture is resolved from the TARGET TOPIC, not from the
#: payload alone (OMN-17209). Kept in a separate registry rather than widening
#: every transform's signature: the three above are field-level rewrites that
#: are correct for any topic declaring them, while ``redact_capture`` reads a
#: per-topic field allowlist out of ``contracts/capture_redaction.yaml`` and
#: has no meaning without one. Separating them also leaves the daemon-side
#: ``PayloadTransform`` signature -- and every existing caller of these three
#: -- byte-unchanged, which is what keeps the OMN-16048 62/62 parity bar green.
TOPIC_SCOPED_TRANSFORM_REGISTRY: dict[str, Callable[[JsonDict, str], JsonDict]] = {
    "redact_capture": redact_capture,
}


def apply_transform(
    transform_name: str | None, payload: JsonDict, *, topic: str | None = None
) -> JsonDict:
    """Apply the named registry transform, refusing an unresolvable name.

    ``None`` and ``passthrough`` resolve to a shallow copy. ``None`` is the
    registry *declaring* that a fan-out rule needs no transform -- the case
    for 60 of the 62 registered events -- so it is a posture, not a miss.

    An unknown NAME is a hard refusal (OMN-17237, H1). This deliberately
    diverges from ``FanOutRule.apply_transform``, which logs a warning and
    falls back to passthrough: that fallback publishes exactly the content
    the named transform exists to remove, so a registry typo or a transform
    declared but never implemented becomes a silent information disclosure.
    The divergence is safe for wire parity because it is unreachable on the
    registry as it stands -- ``test_transform_fail_closed`` asserts every
    ``fan_out[].transform`` in ``topics.yaml`` resolves, so this branch can
    only fire on a name that was never publishable in the first place.

    ``topic`` is required only for a name in
    ``TOPIC_SCOPED_TRANSFORM_REGISTRY`` (OMN-17209): those resolve a per-topic
    capture policy and have no topic-independent posture to fall back on. The
    three field-level transforms ignore it, so every existing caller keeps
    working unchanged.

    Raises:
        UnknownTransformError: ``transform_name`` has no implementation.
        TopicScopedTransformError: a topic-scoped name was called with no
            ``topic``.
    """
    if transform_name is None:
        return dict(payload)
    topic_scoped = TOPIC_SCOPED_TRANSFORM_REGISTRY.get(transform_name)
    if topic_scoped is not None:
        if topic is None:
            raise TopicScopedTransformError(transform_name=transform_name)
        return topic_scoped(payload, topic)
    transform = TRANSFORM_REGISTRY.get(transform_name)
    if transform is None:
        raise UnknownTransformError(
            transform_name,
            tuple(TRANSFORM_REGISTRY) + tuple(TOPIC_SCOPED_TRANSFORM_REGISTRY),
        )
    if transform is transform_passthrough:
        return dict(payload)
    return transform(payload)


# ---------------------------------------------------------------------------
# Partition key
# ---------------------------------------------------------------------------


def derive_partition_key(
    partition_key_field: str | None, payload: JsonDict
) -> str | None:
    """Extract the Kafka partition key, mirroring ``EventRegistry.get_partition_key``.

    ``None`` when the event declares no ``partition_key_field``, or when the
    declared field is absent/null in the payload. Otherwise ``str(value)`` --
    note the daemon stringifies non-string values (e.g. an integer
    ``pr_number``) rather than rejecting them.

    The payload passed here must be the **post-transform** payload: the daemon
    computes the key from ``rule.apply_transform(enriched_payload)``, so a
    transform that drops or rewrites the key field changes the key.
    """
    if partition_key_field is None:
        return None
    value = payload.get(partition_key_field)
    if value is None:
        return None
    return str(value)


__all__: list[str] = [
    "ENRICHMENT_FIELDS",
    "SCHEMA_VERSION",
    "SESSION_ID_ENV_VAR",
    "TOPIC_SCOPED_TRANSFORM_REGISTRY",
    "TRANSFORM_REGISTRY",
    "UNCONDITIONAL_ENRICHMENT_FIELDS",
    "JsonDict",
    "apply_transform",
    "default_clock",
    "default_correlation_id_factory",
    "derive_partition_key",
    "inject_metadata",
    "transform_passthrough",
    "transform_strip_body",
    "transform_strip_prompt",
]
