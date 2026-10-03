# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20117 AC3, proven without restarting a lane: the CLI gets a typed reply.

AC3 asks that a warm runtime restart issued while an ``onex delegate`` request
is in flight returns a typed failed or completed reply to the CLI instead of a
silent timeout. Doing that on a declared lane restarts a shared runtime, so this
test drives the same chain at its seams, with the lab's own recorded shape as
the reference:

1. the real :class:`HandlerDelegateSkill`, cancelled mid-flight the way the
   runtime's shutdown cancels a running dispatch (the seam
   ``test_runtime_shutdown_terminal_omn20117`` pins for the handler alone);
2. the terminal as the runtime publishes it: a ``ModelEventEnvelope`` whose
   payload is the handler's returned terminal, on the failure terminal topic the
   contract declares;
3. the CLI's own terminal intake, ``RuntimeLocal._on_terminal_event`` (what
   ``onex delegate`` runs), and the ``workflow_result.json`` it writes, which is
   the file a lane reads the CLI's exit from.

The reference is the dev lane's own record: delegation_events rows with
``terminal_failure_cause = runtime_shutdown`` joined on correlation id to the CLI
receipts that landed on the lab host, each ``result: failed``, ``exit_code: 1``
and a terminal payload naming ``runtime_shutdown`` for the same correlation id.
A silent timeout is the other shape: ``result: timeout`` and no terminal payload.
Both exit 1, so the exit code alone cannot tell them apart and this test reads
the result and the terminal.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
import yaml
from omnibase_core.enums.enum_terminal_outcome import EnumTerminalOutcome
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.models.runtime.model_contract_terminal_topic import (
    ModelContractTerminalTopic,
)
from omnibase_infra.cli.delegate_terminal_resolver import resolve_delegate_terminal

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillFailed,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers import (
    handler_delegate_skill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_delegation_claim import (
    _CONTRACT_PATH,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

RUNTIME_SHUTDOWN = "runtime_shutdown"

_TERMINAL_TOPICS: dict[str, str] = yaml.safe_load(
    _CONTRACT_PATH.read_text(encoding="utf-8")
)["runtime_dispatch"]["terminal_events"]


class _InFlightDispatchPort:
    """Accepts the dispatch and never resolves: the run the restart cuts off."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def dispatch(self, **_kwargs: Any) -> dict[str, object]:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def _terminal_of_a_run_cut_off_by_shutdown(
    monkeypatch: pytest.MonkeyPatch, request: ModelDelegateSkillRequest
) -> ModelDelegateSkillFailed:
    monkeypatch.setattr(
        handler_delegate_skill,
        "resolve_task_class_execution_budget",
        lambda _task_type: SimpleNamespace(
            task_class_timeout_ceiling_seconds=240,
            terminal_delivery_margin_seconds=30,
        ),
    )
    port = _InFlightDispatchPort()
    handler = HandlerDelegateSkill(dispatch_port=cast("Any", port))
    task = asyncio.create_task(handler.handle(request))
    await asyncio.wait_for(port.started.wait(), timeout=5)
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=5)
    assert done, "the handler must settle promptly once cancelled"
    assert not task.cancelled(), "no terminal was returned for the wiring to publish"
    terminal = task.result()
    assert isinstance(terminal, ModelDelegateSkillFailed)
    return terminal


def _published(terminal: ModelDelegateSkillFailed) -> dict[str, Any]:
    """The failure terminal as it travels the bus: an envelope around the terminal."""
    envelope = ModelEventEnvelope[object](
        envelope_id=uuid4(),
        payload=terminal.model_dump(mode="json"),
        correlation_id=terminal.correlation_id,
        envelope_timestamp=datetime.now(UTC),
        event_type="omnimarket.delegate-skill-failed",
    )
    wire: dict[str, Any] = json.loads(envelope.model_dump_json())
    return wire


def _cli_runtime(tmp_path: Path, request: ModelDelegateSkillRequest) -> Any:
    """The CLI's runtime for one run, awaiting the request's own correlation id."""
    runtime = RuntimeLocal(
        workflow_path=tmp_path / "contract.yaml",
        state_root=tmp_path / "state",
    )
    runtime._expected_correlation_id = request.correlation_id
    return runtime


def _receipt(runtime: Any) -> dict[str, Any]:
    """The file a lane reads the CLI's result and exit from."""
    runtime._write_state()
    receipt: dict[str, Any] = json.loads(
        (runtime.state_root / "workflow_result.json").read_text(encoding="utf-8")
    )
    return receipt


def _request() -> ModelDelegateSkillRequest:
    return ModelDelegateSkillRequest(
        prompt="write a long essay",
        task_type="document",
        source="claude-code",
        correlation_id=uuid4(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "declared_outcome", [EnumTerminalOutcome.FAILURE, EnumTerminalOutcome.SUCCESS]
)
async def test_the_cli_exits_with_the_typed_restart_terminal_for_its_correlation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    declared_outcome: EnumTerminalOutcome,
) -> None:
    """The reply the CLI holds after a restart is the typed failed terminal, not a timeout.

    Delivered on the failure topic and, to prove the status read agrees with the
    topic, on the success topic too: the verdict must be FAILED either way.
    """
    request = _request()
    terminal = await _terminal_of_a_run_cut_off_by_shutdown(monkeypatch, request)
    topic = _TERMINAL_TOPICS[
        "failure" if declared_outcome is EnumTerminalOutcome.FAILURE else "success"
    ]
    runtime = _cli_runtime(tmp_path, request)

    runtime._on_terminal_event(
        _published(terminal),
        ModelContractTerminalTopic(topic=topic, outcome=declared_outcome),
    )
    receipt = _receipt(runtime)

    assert runtime._result is EnumWorkflowResult.FAILED
    assert receipt["result"] == "failed", "a silent timeout would read 'timeout'"
    assert receipt["exit_code"] == 1
    carried = receipt["terminal_payload"]["payload"]
    assert carried["correlation_id"] == str(request.correlation_id)
    assert carried["status"] == "failed"
    assert carried["terminal_failure_cause"] == RUNTIME_SHUTDOWN
    assert "runtime" in carried["error_message"]


@pytest.mark.asyncio
async def test_the_cli_resolves_the_restart_terminal_to_its_typed_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI's own accessor reads the published terminal back with its cause."""
    request = _request()
    terminal = await _terminal_of_a_run_cut_off_by_shutdown(monkeypatch, request)

    resolved = resolve_delegate_terminal(_published(terminal))

    assert resolved.status == "failed"
    assert resolved.terminal_failure_cause == RUNTIME_SHUTDOWN


@pytest.mark.asyncio
async def test_a_restart_terminal_for_another_run_is_not_this_clis_reply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Negative control: the reply is joined on correlation id, not on arrival.

    A restart cuts off every run in flight, so many terminals appear at once. A
    CLI that adopted any of them would hand a caller another run's outcome.
    """
    request = _request()
    other = _request()
    terminal = await _terminal_of_a_run_cut_off_by_shutdown(monkeypatch, other)
    runtime = _cli_runtime(tmp_path, request)

    runtime._on_terminal_event(
        _published(terminal),
        ModelContractTerminalTopic(
            topic=_TERMINAL_TOPICS["failure"], outcome=EnumTerminalOutcome.FAILURE
        ),
    )
    receipt = _receipt(runtime)

    assert receipt["result"] == "timeout"
    assert "terminal_payload" not in receipt


def test_a_silent_timeout_is_a_different_receipt_from_a_typed_failure(
    tmp_path: Path,
) -> None:
    """Positive control for the assertions above: the silent shape is distinguishable.

    With no terminal delivered the CLI's receipt says ``timeout`` and carries no
    terminal. Both outcomes exit 1, which is why AC3 cannot be read off the exit
    code and the tests above read the result and the terminal instead.
    """
    runtime = _cli_runtime(tmp_path, _request())

    receipt = _receipt(runtime)

    assert receipt["result"] == "timeout"
    assert receipt["exit_code"] == 1
    assert "terminal_payload" not in receipt
