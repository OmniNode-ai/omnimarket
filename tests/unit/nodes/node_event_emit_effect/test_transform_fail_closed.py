# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-17237: the emit transform seam must fail CLOSED, not open.

The seam itself landed in OMN-16048 -- ``ResolvedTopic.transform_name``,
``TRANSFORM_REGISTRY``, and ``apply_transform`` applied per fan-out target.
This module covers the three paths that reached the wire WITHOUT entering
that pipeline, each of which published the raw payload verbatim:

H1  an unknown transform name resolved to passthrough (``enrichment.py``:
    ``TRANSFORM_REGISTRY.get(name)`` -> ``None`` -> ``dict(payload)``).
H2  a caller-supplied ``request.topic`` override got ``transform_name=None``
    unconditionally, so ANY override published untransformed -- including on
    the two events (``prompt.submitted``, ``agent.chat.broadcast``) whose
    entire reason for declaring a transform is that their raw payload must
    not reach an observability topic.
H3  a non-dict payload returned early from ``_build_messages`` and was
    published verbatim, un-enriched and un-transformed -- strictly wider than
    the legacy daemon, which REJECTS non-dict payloads outright
    (``socket_server.py``: ``"'payload' must be a JSON object"``).

Every test here asserts the refusal AND that the refusal happened before
anything was spooled: a refused publish must leave zero spool records, or
the next invocation's backlog drain would publish the very payload that was
refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_event_emit_effect.enrichment import (
    TOPIC_SCOPED_TRANSFORM_REGISTRY,
    TRANSFORM_REGISTRY,
    apply_transform,
)
from omnimarket.nodes.node_event_emit_effect.errors import (
    AmbiguousTransformError,
    EmitRedactionError,
    NonMappingPayloadError,
    UnknownTransformError,
    UnresolvableTransformError,
)
from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    JsonType,
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import SpoolOutbox
from omnimarket.nodes.node_event_emit_effect.spool.topic_resolver import (
    default_registry_path,
    resolve_override_transform,
)

pytestmark = pytest.mark.unit


class FakePublishAdapter:
    """Records ``(topic, payload, key)`` per publish; never touches a network.

    Deliberately local rather than imported from a sibling test module: these
    tests assert on what reached the WIRE, so the recorder they read is part
    of the assertion and must not drift under another module's edits.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, JsonType, str | None]] = []

    def publish(
        self,
        topic: str,
        payload: JsonType,
        *,
        key: str | None,
        correlation_id: str | None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.calls.append((topic, payload, key))


def _handler(
    tmp_path: Path,
) -> tuple[HandlerEventEmitEffect, FakePublishAdapter, SpoolOutbox]:
    adapter = FakePublishAdapter()
    spool = SpoolOutbox(tmp_path / "spool")
    return HandlerEventEmitEffect(spool=spool, publish_adapter=adapter), adapter, spool


# ---------------------------------------------------------------------------
# Every refusal is one catchable, DLQ-routable class
# ---------------------------------------------------------------------------


def test_every_redaction_error_shares_one_dlq_routable_base() -> None:
    """A caller routing refusals to a DLQ catches ONE type, not four.

    ``ValueError`` is the base of that base on purpose: the production
    callers (omniclaude's evidence_writer / routing_recorder) already wrap
    their emit in ``except (... ValueError ...)`` and are contractually
    fail-open onto disk. A refusal must degrade them, not crash them.
    """
    for exc_type in (
        UnknownTransformError,
        AmbiguousTransformError,
        UnresolvableTransformError,
        NonMappingPayloadError,
    ):
        assert issubclass(exc_type, EmitRedactionError)
        assert issubclass(exc_type, ValueError)


# ---------------------------------------------------------------------------
# H1: an unknown transform name must refuse, not fall through to passthrough
# ---------------------------------------------------------------------------


def test_unknown_transform_name_refuses_instead_of_passing_through() -> None:
    """RED before the fix: this returned ``dict(payload)`` -- the raw payload.

    A typo in ``topics.yaml``, or a transform named in the registry but never
    implemented, silently published everything the transform existed to strip.
    """
    payload = {"body": "secret", "session_id": "s"}

    with pytest.raises(UnknownTransformError) as excinfo:
        apply_transform("strip_bdoy", payload)  # transposed typo of strip_body

    assert "strip_bdoy" in str(excinfo.value)
    assert excinfo.value.transform_name == "strip_bdoy"
    # The refusal names what IS available, so a registry typo is diagnosable
    # from the error alone.
    assert "strip_body" in str(excinfo.value)


def test_declared_absence_of_a_transform_is_still_passthrough() -> None:
    """``None`` is the registry declaring "no transform", not a lookup miss.

    60 of the 62 registered events declare no ``transform:`` key at all and
    must keep publishing their enriched payload. Inverting H1 must not
    invert this -- otherwise the fix breaks every event in the registry.
    """
    assert apply_transform(None, {"a": 1}) == {"a": 1}
    assert apply_transform("passthrough", {"a": 1}) == {"a": 1}


def test_apply_transform_returns_a_copy_not_the_caller_dict() -> None:
    """One enriched dict is shared across every fan-out target, so a transform
    that mutated it in place would leak one topic's redaction into another."""
    enriched = {"body": "secret"}
    out = apply_transform(None, enriched)
    assert out is not enriched
    out = apply_transform("passthrough", enriched)
    assert out is not enriched


def test_every_transform_declared_in_the_registry_resolves() -> None:
    """The whole point of H1's inversion: prove the registry has no name that
    would now refuse at publish time. This is the gate that turns a runtime
    information-disclosure into a test failure at commit time."""
    registry = yaml.safe_load(default_registry_path().read_text(encoding="utf-8"))
    for event_type, event_def in registry["events"].items():
        for rule in event_def.get("fan_out") or []:
            name = rule.get("transform")
            if name is None:
                continue
            # OMN-17209 added TOPIC_SCOPED_TRANSFORM_REGISTRY: transforms whose
            # posture is resolved from the target topic. A name in either
            # registry resolves; a name in neither refuses at publish time,
            # which is what this gate exists to catch at commit time.
            assert (
                name in TRANSFORM_REGISTRY or name in TOPIC_SCOPED_TRANSFORM_REGISTRY
            ), (
                f"{event_type} -> {rule['topic']} declares transform {name!r}, "
                "which has no implementation; it would refuse at publish time."
            )


# ---------------------------------------------------------------------------
# H2: a topic override must still resolve a redaction posture, or refuse
# ---------------------------------------------------------------------------


def test_override_onto_a_registry_declared_topic_adopts_that_topics_transform(
    tmp_path: Path,
) -> None:
    """RED before the fix: ``transform_name=None`` regardless of the topic.

    ``onex.evt.omniclaude.agent-chat-broadcast.v1`` declares ``strip_body``.
    Reaching it through the override escape hatch published the full ``body``
    -- the exact information-disclosure OMN-16019 named, re-entered through
    a different door.
    """
    handler, adapter, _spool = _handler(tmp_path)
    handler.handle(
        ModelEmitRequest(
            event_type="agent.chat.broadcast",
            payload={"session_id": "s", "agent_id": "a", "body": "secret body"},
            topic="onex.evt.omniclaude.agent-chat-broadcast.v1",
        )
    )

    (_topic, published, _key) = adapter.calls[0]
    assert isinstance(published, dict)
    assert "body" not in published
    assert published["body_length"] == len("secret body")
    assert published["body_preview"] == "secret body"


def test_override_may_not_weaken_the_transform_its_event_declares(
    tmp_path: Path,
) -> None:
    """An override onto an UNREGISTERED topic inherits the event's own posture.

    ``agent.chat.broadcast`` declares exactly one transform (``strip_body``),
    so that posture is unambiguous and follows the payload wherever the
    override points it. Redirecting an event's payload elsewhere must not be
    a way to shed its redaction.
    """
    handler, adapter, _spool = _handler(tmp_path)
    handler.handle(
        ModelEmitRequest(
            event_type="agent.chat.broadcast",
            payload={"session_id": "s", "agent_id": "a", "body": "secret body"},
            topic="onex.evt.omnimarket.some-topic-not-in-the-registry.v1",
        )
    )

    (topic, published, _key) = adapter.calls[0]
    assert topic == "onex.evt.omnimarket.some-topic-not-in-the-registry.v1"
    assert isinstance(published, dict)
    assert "body" not in published
    assert published["body_preview"] == "secret body"


def test_override_refuses_when_the_events_own_transforms_disagree(
    tmp_path: Path,
) -> None:
    """``prompt.submitted`` fans out to a cmd topic with NO transform and an
    evt topic with ``strip_prompt``. An override onto an unregistered topic
    has no way to choose between them -- picking either is a guess, and the
    permissive guess is a prompt leak. Refuse.
    """
    handler, adapter, spool = _handler(tmp_path)

    with pytest.raises(AmbiguousTransformError) as excinfo:
        handler.handle(
            ModelEmitRequest(
                event_type="prompt.submitted",
                payload={"session_id": "s", "prompt_preview": "p", "prompt": "secret"},
                topic="onex.evt.omnimarket.some-topic-not-in-the-registry.v1",
            )
        )

    assert "prompt.submitted" in str(excinfo.value)
    assert adapter.calls == []
    assert spool.pending_count() == 0


def test_override_refuses_when_neither_topic_nor_event_type_is_registered(
    tmp_path: Path,
) -> None:
    """RED before the fix: this published verbatim and was asserted to.

    An unregistered event_type pointed at an unregistered topic has no
    declared redaction posture anywhere. That is precisely the case that must
    fail closed -- the previous behaviour made the override a universal
    bypass of the entire event registry.
    """
    handler, adapter, spool = _handler(tmp_path)

    with pytest.raises(UnresolvableTransformError):
        handler.handle(
            ModelEmitRequest(
                event_type="totally.unregistered.event",
                payload={"x": 1},
                topic="onex.evt.omnimarket.some-topic-not-in-the-registry.v1",
            )
        )

    assert adapter.calls == []
    assert spool.pending_count() == 0


def test_override_refusal_leaves_nothing_on_disk_to_drain_later(
    tmp_path: Path,
) -> None:
    """A refused publish must not be spooled: the backlog drain would publish
    it on the next invocation, turning a refusal into a delay."""
    spool = SpoolOutbox(tmp_path / "spool")
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=FakePublishAdapter())

    with pytest.raises(EmitRedactionError):
        handler.handle(
            ModelEmitRequest(
                event_type="totally.unregistered.event",
                payload={"x": 1},
                topic="onex.evt.omnimarket.some-topic-not-in-the-registry.v1",
            )
        )

    assert spool.pending_count() == 0
    assert spool.list_pending() == []


def test_the_one_production_override_caller_still_resolves() -> None:
    """omniclaude's ``verification/evidence_writer.py`` is the only production
    caller of the override. It emits ``event_type="team.evidence.written"``
    (registered, one fan-out rule, no declared transform) onto
    ``onex.evt.omniclaude.evidence-written.v1`` (not registered).

    Under the H2 rule that resolves to the event's own declared posture --
    "no transform" -- so the caller keeps publishing. This test is the
    regression fence for that caller; if it starts refusing, the emit path
    goes silent for evidence events and only that caller's fail-open
    ``except ValueError`` would notice.
    """
    assert (
        resolve_override_transform(
            "onex.evt.omniclaude.evidence-written.v1",
            "team.evidence.written",
        )
        is None
    )


def test_resolve_override_transform_prefers_the_topics_own_declaration() -> None:
    """Topic-declared posture beats the event's, because the topic is what is
    actually being written to."""
    # OMN-17209 replaced strip_prompt on this rule with the contract-resolved
    # redact_capture. What this test asserts is unchanged: the TOPIC's own
    # declaration wins over the event's other fan-out rule.
    assert (
        resolve_override_transform(
            "onex.evt.omniclaude.prompt-submitted.v1",
            "prompt.submitted",
        )
        == "redact_capture"
    )
    # Same event, its other fan-out topic, which declares no transform.
    assert (
        resolve_override_transform(
            "onex.cmd.omniintelligence.claude-hook-event.v1",
            "prompt.submitted",
        )
        is None
    )


# ---------------------------------------------------------------------------
# H3: a non-dict payload must go through the pipeline or be refused
# ---------------------------------------------------------------------------


def test_non_dict_payload_refuses_instead_of_publishing_verbatim(
    tmp_path: Path,
) -> None:
    """RED before the fix: a list payload was published verbatim and asserted
    to be. A bare string is the natural shape for captured command output,
    which is exactly the content a redaction contract exists to hold."""
    handler, adapter, spool = _handler(tmp_path)

    with pytest.raises(NonMappingPayloadError) as excinfo:
        handler.handle(
            ModelEmitRequest(event_type="session.started", payload=["a", "b"])
        )

    assert "list" in str(excinfo.value)
    assert adapter.calls == []
    assert spool.pending_count() == 0


@pytest.mark.parametrize("payload", ["raw command output", 7, 1.5, True])
def test_every_scalar_payload_shape_refuses(payload: object, tmp_path: Path) -> None:
    handler, adapter, spool = _handler(tmp_path)

    with pytest.raises(NonMappingPayloadError):
        handler.handle(ModelEmitRequest(event_type="session.started", payload=payload))

    assert adapter.calls == []
    assert spool.pending_count() == 0


def test_null_payload_is_the_empty_object_not_a_refusal(tmp_path: Path) -> None:
    """The daemon this node is at parity with maps a null payload to ``{}``
    (``socket_server.py``: ``if raw_payload is None: raw_payload = {}``) and
    only then rejects non-objects. Refusing null here would be a parity
    regression, not a redaction win -- an empty object has no content to
    disclose."""
    handler, adapter, _spool = _handler(tmp_path)
    handler.handle(ModelEmitRequest(event_type="session.started", payload=None))

    (_topic, published, _key) = adapter.calls[0]
    assert isinstance(published, dict)
    # Enriched, i.e. it went THROUGH the pipeline rather than around it.
    assert published["schema_version"] == "1.0.0"
    assert "correlation_id" in published


def test_non_dict_refusal_precedes_topic_resolution(tmp_path: Path) -> None:
    """Payload shape is refused on its own terms, not as a side effect of a
    topic lookup -- so it still refuses on a fully resolvable event."""
    handler, adapter, spool = _handler(tmp_path)

    with pytest.raises(NonMappingPayloadError):
        handler.handle(
            ModelEmitRequest(
                event_type="agent.chat.broadcast",
                payload="secret body as a bare string",
                topic="onex.evt.omniclaude.agent-chat-broadcast.v1",
            )
        )

    assert adapter.calls == []
    assert spool.pending_count() == 0
