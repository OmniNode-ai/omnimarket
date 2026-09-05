# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Typed refusals for the ``node_event_emit_effect`` redaction seam (OMN-17237).

Every error here means the same thing: **this payload was not published,
because its redaction posture could not be established.** They exist because
the three paths below previously reached the wire by falling through to a
verbatim publish, which is the failure direction that discloses information.

Catching one class
------------------
All four derive from :class:`EmitRedactionError`, so a caller routing refused
publishes to a DLQ catches exactly one type and reads the structured
attributes off it (``event_type`` / ``topic`` / ``transform_name`` /
``payload_type``) rather than parsing a message.

:class:`EmitRedactionError` derives from ``ValueError`` deliberately. The
production callers of this handler -- ``omniclaude``'s
``verification/evidence_writer.py`` and ``routing/routing_recorder.py`` --
already wrap their emit in ``except (..., ValueError, ...)`` because Kafka is
best-effort for them and disk is authoritative. A refusal must DEGRADE those
callers (event not published, disk record intact), not crash the hook they run
inside. Widening the base to ``Exception`` would turn a redaction refusal into
a caller-visible crash; narrowing it to a bespoke root they do not catch would
do the same.
"""

from __future__ import annotations


class EmitRedactionError(ValueError):
    """Base: a publish was refused because redaction could not be established.

    Never raised directly -- catch it, and raise one of its subclasses.
    """


class UnknownTransformError(EmitRedactionError):
    """A transform NAME does not resolve to an implementation (H1).

    A registry typo, or a transform declared in ``topics.yaml`` but never
    implemented. Previously this fell through to passthrough, publishing
    exactly the content the named transform existed to remove.

    Distinct from a transform name of ``None``, which is the registry
    *declaring* that a fan-out rule needs no transform -- the case for 60 of
    the 62 registered events, and not an error.
    """

    def __init__(self, transform_name: str, available: tuple[str, ...]) -> None:
        self.transform_name = transform_name
        self.available = available
        super().__init__(
            f"Unknown transform {transform_name!r}: no implementation in "
            f"TRANSFORM_REGISTRY (available: {', '.join(sorted(available))}). "
            "Refusing to publish -- an unresolvable transform previously fell "
            "back to passthrough, which publishes the payload the transform "
            "exists to redact."
        )


class AmbiguousTransformError(EmitRedactionError):
    """Two declarations disagree about a payload's redaction posture (H2).

    Raised when a topic override cannot be resolved to ONE posture: either the
    override topic is declared by several fan-out rules that name different
    transforms, or the event's own fan-out rules disagree (``prompt.submitted``
    is the live example -- its cmd topic declares no transform, its evt topic
    declares ``strip_prompt``).

    Choosing between them would be a guess, and the permissive guess is the
    disclosure. Refuse instead.
    """

    def __init__(
        self,
        *,
        topic: str,
        event_type: str,
        candidates: tuple[str | None, ...],
        source: str,
    ) -> None:
        self.topic = topic
        self.event_type = event_type
        self.candidates = candidates
        self.source = source
        rendered = ", ".join(
            "<no transform>" if c is None else repr(c)
            for c in sorted(candidates, key=lambda c: (c is not None, c or ""))
        )
        super().__init__(
            f"Topic override {topic!r} for event_type {event_type!r} has no "
            f"single redaction posture: {source} declares {rendered}. Refusing "
            "to publish -- declare this topic in the event registry with the "
            "transform it requires."
        )


class UnresolvableTransformError(EmitRedactionError):
    """No declaration anywhere covers this publish (H2).

    The override topic is absent from the registry AND its ``event_type`` is
    unregistered (or registered with no fan-out rules), so nothing declares
    what may reach that topic. Previously this published verbatim, which made
    the override field a universal bypass of the entire event registry.
    """

    def __init__(self, *, topic: str, event_type: str, reason: str) -> None:
        self.topic = topic
        self.event_type = event_type
        self.reason = reason
        super().__init__(
            f"Topic override {topic!r} for event_type {event_type!r} has no "
            f"declared redaction posture: {reason}. Refusing to publish -- "
            "register the topic in the event registry (or emit under a "
            "registered event_type) rather than routing around it."
        )


class NonMappingPayloadError(EmitRedactionError):
    """The payload has no field surface, so no transform can be applied (H3).

    The transforms are field-level operations (drop ``body``, truncate
    ``prompt_preview``). A list or a bare string cannot be inspected by them,
    so such a payload previously bypassed enrichment, transform and keying
    entirely and went out verbatim -- while a bare string is exactly the shape
    captured command output arrives in.

    The legacy ``node_emit_daemon`` this node reproduces already rejects these
    (``socket_server._handle_emit``: ``"'payload' must be a JSON object"``), so
    refusing here narrows the node back to daemon parity rather than
    introducing a new restriction. A ``None`` payload is NOT this error: the
    daemon maps it to ``{}`` first, and an empty object discloses nothing.
    """

    def __init__(self, *, event_type: str, payload_type: str) -> None:
        self.event_type = event_type
        self.payload_type = payload_type
        super().__init__(
            f"Payload for event_type {event_type!r} is a {payload_type}, not a "
            "JSON object. Refusing to publish -- a payload with no field "
            "surface cannot be transformed, and previously bypassed the "
            "redaction pipeline entirely."
        )


class MalformedRedactionContractError(EmitRedactionError):
    """The capture redaction contract cannot be resolved (OMN-17209).

    A missing key, an unknown capture class, a pattern that does not compile,
    an output class with no stated reason, or a governed topic with an empty
    field policy. All of them mean the same thing: nobody can say what this
    publish is allowed to carry, so it does not happen.

    Deliberately NOT recoverable by falling back to "hash everything". A
    record hashed by accident is indistinguishable on the wire from one hashed
    by a reviewed decision, and the difference is the whole control.
    """

    def __init__(self, *, source: str, detail: str) -> None:
        self.source = source
        self.detail = detail
        super().__init__(
            f"Capture redaction contract at {source} is unusable: {detail}. "
            "Refusing to publish -- the contract is the redaction posture, so "
            "an unresolvable contract is an unknown posture, not a default one."
        )


class UngovernedTopicError(EmitRedactionError):
    """A fan-out rule names ``redact_capture`` for a topic the contract omits.

    The permissive reading -- apply the fail-closed default to every field --
    would publish a record that LOOKS redacted while no one has reviewed what
    it carries, and would silently hash the partition key. Refuse instead, and
    make the author add the topic to ``contracts/capture_redaction.yaml``.
    """

    def __init__(
        self, *, topic: str, governed: tuple[str, ...], contract_path: str
    ) -> None:
        self.topic = topic
        self.governed = governed
        self.contract_path = contract_path
        super().__init__(
            f"Topic {topic!r} names the redact_capture transform but is not "
            f"governed by {contract_path} (governed: {', '.join(governed)}). "
            "Refusing to publish -- declare the topic's per-field capture "
            "classes in the contract rather than relying on the default."
        )


class TopicScopedTransformError(EmitRedactionError):
    """A topic-scoped transform was invoked with no target topic (OMN-17209).

    ``redact_capture`` resolves its policy from the TOPIC it is publishing to,
    because the contract is declared per topic. A caller that reaches
    ``apply_transform`` without one has lost the fan-out target somewhere
    upstream; guessing a topic would pick a policy at random.
    """

    def __init__(self, *, transform_name: str) -> None:
        self.transform_name = transform_name
        super().__init__(
            f"Transform {transform_name!r} is topic-scoped and was called with "
            "topic=None. Refusing to publish -- its capture policy is resolved "
            "per topic, so there is no topic-independent posture to fall back on."
        )


__all__: list[str] = [
    "AmbiguousTransformError",
    "EmitRedactionError",
    "MalformedRedactionContractError",
    "NonMappingPayloadError",
    "TopicScopedTransformError",
    "UngovernedTopicError",
    "UnknownTransformError",
    "UnresolvableTransformError",
]
