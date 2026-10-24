# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A failed build replies native and publishes fallback; a built one installs (OMN-21113, TA.11)."""

from __future__ import annotations

import asyncio
import hashlib
from unittest.mock import Mock
from uuid import UUID

import pytest
from omnibase_core.enums.enum_client_verdict_action import EnumClientVerdictAction
from pydantic import ValidationError

from omnimarket.models.context_compaction import (
    EnumContextCompactionFailureClass,
    EnumContextCompactionTrigger,
    ModelContextCompactionActivated,
    ModelContextCompactionBuildOutcome,
    ModelContextCompactionBuiltPackage,
    ModelContextCompactionFallback,
    ModelContextCompactionPackageReply,
    ModelContextCompactionPrepared,
    ModelContextCompactionRequested,
    ModelContextCompactionResult,
    installed_messages_hash,
)
from omnimarket.models.context_pack_orchestrator import (
    EnumContextPackOrchestratorStatus,
)
from omnimarket.models.context_pack_render.model_context_pack_render_message import (
    ModelContextPackRenderMessage,
)
from omnimarket.models.model_session_stream_barrier import (
    ModelSessionStreamBarrierVerdict,
)
from omnimarket.nodes.node_context_compaction_orchestrator.event_topics import (
    publish_topic_for,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers.handler_context_compaction_orchestrator import (
    HandlerContextCompactionOrchestrator,
)

from .bus_fixture import CannedPack
from .test_compaction_pack_build import _ok_built

pytestmark = pytest.mark.unit

INVOCATION = UUID("11111111-1111-4111-8111-111111111111")
CANDIDATE = UUID("22222222-2222-5222-8222-222222222222")
ACTIVATION = UUID("33333333-3333-4333-8333-333333333333")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _request(compact_model: str | None = None) -> ModelContextCompactionRequested:
    return ModelContextCompactionRequested(
        invocation_id=INVOCATION,
        session_id="session-1",
        cwd="/work/repo",
        trigger=EnumContextCompactionTrigger.MANUAL,
        input_messages_hash=_sha("input"),
        newest_turn="turn-9",
        compact_model=compact_model,
        barrier_id="barrier-1",
    )


def _package() -> ModelContextCompactionBuiltPackage:
    return ModelContextCompactionBuiltPackage(
        candidate_id=CANDIDATE,
        assembly_hash=_sha("assembly"),
        model_outputs_hash=_sha("model-outputs"),
        final_pack_hash=_sha("final"),
        body_hash=_sha("body"),
        version_vector_hash=_sha("vector"),
        profile_name="compaction_orchestrator",
        profile_content_hash=_sha("profile"),
        messages=(
            ModelContextPackRenderMessage(role="user", text="legend"),
            ModelContextPackRenderMessage(role="user", text="identity trailer"),
        ),
    )


def _bus(compaction_bus, *, failed=False, raising=False, **kwargs):
    built = _ok_built(
        **{
            k: v
            for k, v in _package().model_dump(mode="json").items()
            if k not in {"model_outputs_hash", "model_section_ms"}
        }
    )
    bus = compaction_bus(CannedPack(built))
    if failed:
        bus.pack.built = bus.pack.built.model_copy(
            update={"status": EnumContextPackOrchestratorStatus.FAILED}
        )
    if raising:

        def publish(topic, payload):
            raise RuntimeError("publish failed")

        kwargs["event_publisher"] = publish
    return bus, bus.handler(**kwargs)


def _assert_native_fallback(
    result: ModelContextCompactionResult,
    failure_class: EnumContextCompactionFailureClass,
) -> None:
    assert result.reply == ModelContextCompactionPackageReply(
        invocation_id=INVOCATION, action=EnumClientVerdictAction.NATIVE
    )
    assert result.reply.messages == ()
    assert result.reply.installed_messages_hash is None
    assert result.activated is None
    assert result.fallback is not None
    assert result.fallback.build_ms is not None
    assert result.fallback == ModelContextCompactionFallback(
        invocation_id=INVOCATION,
        session_id="session-1",
        failure_class=failure_class,
        build_ms=result.fallback.build_ms,
    )


@pytest.mark.parametrize("boundary", [False, True])
def test_a_failed_build_replies_native_and_publishes_fallback_with_class_and_invocation_id(
    compaction_bus, boundary
):
    bus, handler = _bus(compaction_bus, failed=True)
    if boundary:
        # An invalid typed payload covers the boundary terminal shape through transport.
        def publish(topic, payload):
            import json

            body = json.loads(payload)
            body["payload"] = {"status": "failed", "error": "boundary failure"}
            bus.publisher(bus.topics.failed_topic, json.dumps(body).encode())

        handler = bus.handler(event_publisher=publish)
    result = handler.handle(_request())
    _assert_native_fallback(result, EnumContextCompactionFailureClass.PACK_BUILD_FAILED)


def test_a_raising_build_replies_native_with_build_error(compaction_bus):
    _, handler = _bus(compaction_bus, raising=True)
    _assert_native_fallback(
        handler.handle(_request()), EnumContextCompactionFailureClass.BUILD_ERROR
    )


def test_installed_package_records_build_and_model_section_timings(compaction_bus):
    clock = Mock(side_effect=[1.0, 1.25])
    _, handler = _bus(compaction_bus, clock=clock)
    result = handler.handle(_request())
    assert result.prepared is not None
    assert result.prepared.build_ms == 250.0
    # The package mapping has always left model_section_ms at its default.
    assert result.prepared.model_section_ms is None
    assert clock.call_count == 2


@pytest.mark.parametrize("raises", [False, True])
def test_failed_build_records_elapsed_time(compaction_bus, raises):
    clock = Mock(side_effect=[1.0, 1.25])
    _, handler = _bus(compaction_bus, failed=not raises, raising=raises, clock=clock)
    result = handler.handle(_request())
    assert result.fallback.build_ms == 250.0
    assert result.fallback.failure_class is (
        EnumContextCompactionFailureClass.BUILD_ERROR
        if raises
        else EnumContextCompactionFailureClass.PACK_BUILD_FAILED
    )
    assert clock.call_count == 2


def test_unmet_barrier_does_not_time_or_run_a_build(compaction_bus) -> None:
    barrier_wait = Mock()
    barrier_wait.wait.return_value = ModelSessionStreamBarrierVerdict(
        session_id="session-1", barrier_id="barrier-1", outcome="not_met", streams=()
    )
    clock = Mock(side_effect=AssertionError("no build ran"))
    bus, handler = _bus(compaction_bus, barrier_wait=barrier_wait, clock=clock)
    result = handler.handle(_request())
    assert result.fallback is not None
    assert (
        result.fallback.failure_class
        is EnumContextCompactionFailureClass.SOURCE_NOT_CAUGHT_UP
    )
    assert result.fallback.build_ms is None
    assert not bus.commands
    clock.assert_not_called()
    barrier_wait.wait.assert_called_once()


def test_the_unwired_default_build_falls_back_to_native() -> None:
    result = HandlerContextCompactionOrchestrator().handle(_request())
    _assert_native_fallback(
        result, EnumContextCompactionFailureClass.PACK_BUILDER_UNAVAILABLE
    )


def test_a_built_package_installs_with_prepared_and_activated_receipts(
    compaction_bus,
) -> None:
    _, handler = _bus(
        compaction_bus,
        activation_ids=lambda: ACTIVATION,
        clock=Mock(side_effect=[1.0, 1.25]),
    )
    result = handler.handle(_request())

    assert result.fallback is None
    assert result.reply.action is EnumClientVerdictAction.INSTALL
    assert result.reply.invocation_id == INVOCATION
    texts = [m.text for m in result.reply.messages]
    assert texts == ["legend", f"identity trailer\nactivation_id: {ACTIVATION}"]
    assert result.reply.installed_messages_hash == installed_messages_hash(
        result.reply.messages
    )
    assert result.prepared == ModelContextCompactionPrepared(
        candidate_id=CANDIDATE,
        session_id="session-1",
        input_messages_hash=_sha("input"),
        assembly_hash=_sha("assembly"),
        version_vector_hash=_sha("vector"),
        profile_name="compaction_orchestrator",
        profile_content_hash=_sha("profile"),
        build_ms=250.0,
    )
    activated = result.activated
    assert activated is not None
    assert activated.activation_id == ACTIVATION
    assert activated.candidate_id == CANDIDATE
    assert activated.invocation_id == INVOCATION
    assert activated.trigger is EnumContextCompactionTrigger.MANUAL
    assert activated.final_pack_hash == _sha("final")
    assert activated.installed_messages_hash == result.reply.installed_messages_hash


def test_two_invocations_of_one_candidate_get_distinct_activations(
    compaction_bus,
) -> None:
    ids = iter([ACTIVATION, UUID("44444444-4444-4444-8444-444444444444")])
    _, handler = _bus(
        compaction_bus,
        activation_ids=lambda: next(ids),
        clock=lambda: 0.0,
    )
    first, second = handler.handle(_request()), handler.handle(_request())
    assert first.prepared == second.prepared
    assert first.activated is not None
    assert second.activated is not None
    assert first.activated.activation_id != second.activated.activation_id


def test_receipts_carry_no_free_text_content() -> None:
    """Receipt fields are ids, hashes, names and enums; no message text field exists."""
    for model in (
        ModelContextCompactionPrepared,
        ModelContextCompactionActivated,
        ModelContextCompactionFallback,
    ):
        assert not {"messages", "text", "content", "prompt"} & set(model.model_fields)


def test_an_outcome_is_a_package_or_a_failure_never_both_or_neither() -> None:
    with pytest.raises(ValidationError):
        ModelContextCompactionBuildOutcome()
    with pytest.raises(ValidationError):
        ModelContextCompactionBuildOutcome(
            package=_package(),
            failure_class=EnumContextCompactionFailureClass.PACK_BUILD_FAILED,
        )


def test_a_native_reply_cannot_carry_a_package() -> None:
    with pytest.raises(ValidationError):
        ModelContextCompactionPackageReply(
            invocation_id=INVOCATION,
            action=EnumClientVerdictAction.NATIVE,
            messages=(ModelContextPackRenderMessage(role="user", text="x"),),
            installed_messages_hash=_sha("x"),
        )
    with pytest.raises(ValidationError):
        ModelContextCompactionPackageReply(
            invocation_id=INVOCATION, action=EnumClientVerdictAction.ANSWER
        )


def test_the_runtime_output_publishes_fallback_then_the_native_reply() -> None:
    output = asyncio.run(
        HandlerContextCompactionOrchestrator().handle_async(_request())
    )
    assert output.correlation_id == INVOCATION
    assert [publish_topic_for(e) for e in output.events] == [
        "onex.evt.omnimarket.context-compaction-fallback.v1",
        "onex.cmd.omniintelligence.context-compaction-package.v1",
    ]
    reply = output.events[-1]
    assert isinstance(reply, ModelContextCompactionPackageReply)
    assert reply.action is EnumClientVerdictAction.NATIVE


def test_the_runtime_output_publishes_receipts_before_the_install_reply(
    compaction_bus,
) -> None:
    _, handler = _bus(
        compaction_bus,
        activation_ids=lambda: ACTIVATION,
    )
    output = asyncio.run(handler.handle_async(_request()))
    assert [publish_topic_for(e) for e in output.events] == [
        "onex.evt.omnimarket.context-compaction-prepared.v1",
        "onex.evt.omnimarket.context-compaction-activated.v1",
        "onex.cmd.omniintelligence.context-compaction-package.v1",
    ]
