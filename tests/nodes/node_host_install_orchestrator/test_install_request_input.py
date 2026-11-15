# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The orchestrator's second typed input: the install request for a tree (OMN-24546).

The typed install request a lane publishes, and the result the per-host install request effect answers with, are
routed like every other payload the orchestrator consumes: the request is remembered by id and publishes nothing, the
result ends the request with the one terminal event, and a result for a request the orchestrator does not know is
ignored.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from omnimarket.models.install_request import (
    EnumInstallRequestStatus,
    EnumInstallTarget,
    ModelInstallRequest,
    ModelInstallRequestResult,
)
from omnimarket.nodes.node_host_install_orchestrator.handlers.handler_host_install_orchestrator import (
    TOPIC_COMPLETED,
    HandlerHostInstallOrchestrator,
    ModelHostInstallOutput,
)
from omnimarket.nodes.node_host_install_orchestrator.models import (
    ModelHostInstallCompleted,
)
from omnimarket.nodes.node_host_install_state_reducer.models import (
    EnumHostInstallState,
)
from tests.nodes.node_host_install_orchestrator.support import (
    START,
    StepClock,
    orchestrator_overlay,
)

pytestmark = pytest.mark.unit

HOST = "host-a"
UNIT = "unit-a"
REVISION = "9f2c1ab"
VERIFY_LINE = "unit-a is running"


def tree_request(
    kind: EnumInstallTarget = EnumInstallTarget.UNIT_TREE,
    request_id: UUID | None = None,
) -> ModelInstallRequest:
    return ModelInstallRequest(
        lane="lane-test",
        host=HOST,
        kind=kind,
        repository="omninode/example",
        ref=REVISION,
        unit=UNIT,
        verify="verify-example",
        request_id=request_id or uuid4(),
        requested_at=START,
        expires_at=START + timedelta(hours=1),
    )


def tree_result(
    request_id: UUID,
    status: EnumInstallRequestStatus,
    *,
    reason: str = "ok",
    revision: str = "",
    verify_line: str = "",
    rolled_back: bool = False,
) -> ModelInstallRequestResult:
    return ModelInstallRequestResult(
        request_id=request_id,
        host=HOST,
        unit=UNIT,
        status=status,
        at=START,
        revision=revision,
        verify_line=verify_line,
        reason=reason,
        rolled_back=rolled_back,
    )


def _completed(outputs: object) -> ModelHostInstallCompleted:
    assert isinstance(outputs, tuple)
    assert len(outputs) == 1
    payload = outputs[0].payload
    assert isinstance(payload, ModelHostInstallCompleted)
    return payload


def test_golden_a_request_then_its_verified_result_ends_completed_installed() -> None:
    orchestrator = HandlerHostInstallOrchestrator(orchestrator_overlay(), StepClock())
    ask = tree_request()
    assert orchestrator.decide(ask, ask.request_id) == ()
    outputs = orchestrator.decide(
        tree_result(
            ask.request_id,
            EnumInstallRequestStatus.VERIFIED,
            revision=REVISION,
            verify_line=VERIFY_LINE,
        ),
        ask.request_id,
    )
    assert [out.topic for out in outputs] == [TOPIC_COMPLETED]
    completed = _completed(outputs)
    assert completed.correlation_id == ask.request_id
    assert completed.host == HOST
    assert completed.unit == UNIT
    assert completed.final_state is EnumHostInstallState.RUN_OK
    assert completed.outcome == "installed"
    assert completed.installed_digest == ""  # a commit sha is not a digest
    assert completed.detail == f"verified:ok revision={REVISION} {VERIFY_LINE}"
    assert orchestrator.known_installs == ()


def test_error_chain_a_refused_result_ends_failed() -> None:
    orchestrator = HandlerHostInstallOrchestrator(orchestrator_overlay(), StepClock())
    ask = tree_request()
    orchestrator.decide(ask, ask.request_id)
    outputs = orchestrator.decide(
        tree_result(
            ask.request_id,
            EnumInstallRequestStatus.REFUSED,
            reason="repository_not_allowed",
        ),
        ask.request_id,
    )
    completed = _completed(outputs)
    assert completed.correlation_id == ask.request_id
    assert completed.final_state is EnumHostInstallState.FAILED
    assert completed.outcome == "failed"
    assert completed.detail.startswith("refused:repository_not_allowed")
    assert orchestrator.known_installs == ()


def test_error_chain_a_verify_failed_result_that_rolled_back_ends_rolled_back() -> None:
    orchestrator = HandlerHostInstallOrchestrator(orchestrator_overlay(), StepClock())
    ask = tree_request()
    orchestrator.decide(ask, ask.request_id)
    outputs = orchestrator.decide(
        tree_result(
            ask.request_id,
            EnumInstallRequestStatus.VERIFY_FAILED,
            reason="verify_mismatch",
            rolled_back=True,
        ),
        ask.request_id,
    )
    completed = _completed(outputs)
    assert completed.final_state is EnumHostInstallState.ROLLED_BACK
    assert completed.outcome == "rolled_back"
    assert completed.detail.startswith("verify_failed:verify_mismatch")
    assert orchestrator.known_installs == ()


def test_error_chain_a_result_for_an_unknown_request_is_ignored() -> None:
    orchestrator = HandlerHostInstallOrchestrator(orchestrator_overlay(), StepClock())
    ask = tree_request()
    orchestrator.decide(ask, ask.request_id)
    assert (
        orchestrator.decide(
            tree_result(
                uuid4(),
                EnumInstallRequestStatus.VERIFY_FAILED,
                reason="verify_mismatch",
                rolled_back=True,
            ),
            uuid4(),
        )
        == ()
    )
    assert orchestrator.known_installs == (ask.request_id,)


def test_a_request_alone_publishes_nothing() -> None:
    orchestrator = HandlerHostInstallOrchestrator(orchestrator_overlay(), StepClock())
    ask = tree_request(kind=EnumInstallTarget.PLUGIN_TREE)
    assert orchestrator.decide(ask, ask.request_id) == ()
    assert orchestrator.known_installs == (ask.request_id,)


def test_tree_requests_drop_the_oldest_past_the_limit() -> None:
    overlay = orchestrator_overlay().model_copy(update={"context_limit": 2})
    orchestrator = HandlerHostInstallOrchestrator(overlay, StepClock())
    ids = [uuid4() for _ in range(3)]
    for request_id in ids:
        orchestrator.decide(tree_request(request_id=request_id), request_id)
    assert orchestrator.known_installs == (ids[1], ids[2])


class _WithoutIgnore(HandlerHostInstallOrchestrator):
    """The failing control: the same handler with the unknown-request ignore removed."""

    def _on_install_result(
        self, result: ModelInstallRequestResult
    ) -> tuple[ModelHostInstallOutput, ...]:
        self._tree_requests.pop(result.request_id, None)
        return (self._tree_completed(result),)


def test_failing_control_without_the_ignore_an_unknown_result_would_publish() -> None:
    orchestrator = _WithoutIgnore(orchestrator_overlay(), StepClock())
    result = tree_result(
        uuid4(),
        EnumInstallRequestStatus.REFUSED,
        reason="repository_not_allowed",
    )
    outputs = orchestrator.decide(result, uuid4())
    assert [out.topic for out in outputs] == [TOPIC_COMPLETED]
