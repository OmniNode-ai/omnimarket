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


class _Build:
    def __init__(self, outcome: ModelContextCompactionBuildOutcome) -> None:
        self.outcome = outcome
        self.seen: list[ModelContextCompactionRequested] = []

    def build(
        self, request: ModelContextCompactionRequested
    ) -> ModelContextCompactionBuildOutcome:
        self.seen.append(request)
        return self.outcome


class _RaisingBuild:
    def build(
        self, request: ModelContextCompactionRequested
    ) -> ModelContextCompactionBuildOutcome:
        raise RuntimeError("source read failed")


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


@pytest.mark.parametrize(
    "failure_class",
    [
        EnumContextCompactionFailureClass.PACK_BUILD_FAILED,
        EnumContextCompactionFailureClass.SOURCE_NOT_CAUGHT_UP,
        EnumContextCompactionFailureClass.SOURCE_INCOMPLETE,
        EnumContextCompactionFailureClass.SOURCE_DISABLED,
        EnumContextCompactionFailureClass.NO_SOURCE_TIMESTAMP,
    ],
)
def test_a_failed_build_replies_native_and_publishes_fallback_with_class_and_invocation_id(
    failure_class: EnumContextCompactionFailureClass,
) -> None:
    build = _Build(ModelContextCompactionBuildOutcome(failure_class=failure_class))
    result = HandlerContextCompactionOrchestrator(build).handle(_request())
    _assert_native_fallback(result, failure_class)
    assert build.seen == [_request()]


def test_a_raising_build_replies_native_with_build_error() -> None:
    result = HandlerContextCompactionOrchestrator(_RaisingBuild()).handle(_request())
    _assert_native_fallback(result, EnumContextCompactionFailureClass.BUILD_ERROR)


def test_installed_package_records_build_and_model_section_timings() -> None:
    package = ModelContextCompactionBuiltPackage.model_validate(
        {**_package().model_dump(), "model_section_ms": 75.0}
    )
    clock = Mock(side_effect=[1.0, 1.25])
    result = HandlerContextCompactionOrchestrator(
        _Build(ModelContextCompactionBuildOutcome(package=package)), clock=clock
    ).handle(_request())
    assert result.prepared is not None
    assert result.prepared.build_ms == 250.0
    assert result.prepared.model_section_ms == package.model_section_ms
    assert clock.call_count == 2


@pytest.mark.parametrize("raises", [False, True])
def test_failed_build_records_elapsed_time(raises: bool) -> None:
    build = (
        _RaisingBuild()
        if raises
        else _Build(
            ModelContextCompactionBuildOutcome(
                failure_class=EnumContextCompactionFailureClass.PACK_BUILD_FAILED
            )
        )
    )
    clock = Mock(side_effect=[1.0, 1.25])
    result = HandlerContextCompactionOrchestrator(build, clock=clock).handle(_request())
    assert result.fallback is not None
    assert result.fallback.build_ms == 250.0
    assert result.fallback.failure_class is (
        EnumContextCompactionFailureClass.BUILD_ERROR
        if raises
        else EnumContextCompactionFailureClass.PACK_BUILD_FAILED
    )
    assert clock.call_count == 2


def test_unmet_barrier_does_not_time_or_run_a_build() -> None:
    build = _Build(ModelContextCompactionBuildOutcome(package=_package()))
    barrier_wait = Mock()
    barrier_wait.wait.return_value = ModelSessionStreamBarrierVerdict(
        session_id="session-1", barrier_id="barrier-1", outcome="not_met", streams=()
    )
    clock = Mock(side_effect=AssertionError("no build ran"))
    result = HandlerContextCompactionOrchestrator(
        build, barrier_wait=barrier_wait, clock=clock
    ).handle(_request().model_copy(update={"barrier_id": "barrier-1"}))
    assert result.fallback is not None
    assert (
        result.fallback.failure_class
        is EnumContextCompactionFailureClass.SOURCE_NOT_CAUGHT_UP
    )
    assert result.fallback.build_ms is None
    assert not build.seen
    clock.assert_not_called()
    barrier_wait.wait.assert_called_once()


def test_the_unwired_default_build_falls_back_to_native() -> None:
    result = HandlerContextCompactionOrchestrator().handle(_request())
    _assert_native_fallback(
        result, EnumContextCompactionFailureClass.PACK_BUILDER_UNAVAILABLE
    )


def test_a_built_package_installs_with_prepared_and_activated_receipts() -> None:
    build = _Build(ModelContextCompactionBuildOutcome(package=_package()))
    result = HandlerContextCompactionOrchestrator(
        build, activation_ids=lambda: ACTIVATION, clock=Mock(side_effect=[1.0, 1.25])
    ).handle(_request())

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


def test_two_invocations_of_one_candidate_get_distinct_activations() -> None:
    ids = iter([ACTIVATION, UUID("44444444-4444-4444-8444-444444444444")])
    handler = HandlerContextCompactionOrchestrator(
        _Build(ModelContextCompactionBuildOutcome(package=_package())),
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


def test_the_runtime_output_publishes_receipts_before_the_install_reply() -> None:
    handler = HandlerContextCompactionOrchestrator(
        _Build(ModelContextCompactionBuildOutcome(package=_package())),
        activation_ids=lambda: ACTIVATION,
    )
    output = asyncio.run(handler.handle_async(_request()))
    assert [publish_topic_for(e) for e in output.events] == [
        "onex.evt.omnimarket.context-compaction-prepared.v1",
        "onex.evt.omnimarket.context-compaction-activated.v1",
        "onex.cmd.omniintelligence.context-compaction-package.v1",
    ]
