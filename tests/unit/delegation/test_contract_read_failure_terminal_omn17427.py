# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An unreadable ingress contract answers the caller before any model runs."""

from __future__ import annotations

import errno
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as workflow_module,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationFailed,
)

pytestmark = pytest.mark.unit


def _request() -> ModelDelegationRequest:
    return ModelDelegationRequest(
        correlation_id=uuid4(),
        task_type="document",
        prompt="Write a PR body from the supplied facts.",
        tenant_id="tenant-for-contract-failure",
        emitted_at=datetime.now(UTC),
    )


@pytest.mark.parametrize("error_number", [errno.EMFILE, errno.ENOENT, errno.EACCES])
@pytest.mark.parametrize("explicit_contract", [False, True])
async def test_contract_read_failure_returns_unscored_terminal(
    monkeypatch: pytest.MonkeyPatch, error_number: int, explicit_contract: bool
) -> None:
    request = _request()
    if explicit_contract:
        request = request.model_copy(update={"response_contract": {"type": "object"}})
    handler = HandlerDelegationWorkflow(workflows={})
    original = Path.read_text

    def unreadable(path: Path, *args: object, **kwargs: object) -> str:
        if path.name == "task_class_contracts.v1.yaml":
            raise OSError(error_number, "contract read refused", str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    events = await handler.handle(request)
    terminals = [event for event in events if isinstance(event, ModelDelegationFailed)]
    assert len(terminals) == 1
    terminal = terminals[0]
    assert terminal.correlation_id == request.correlation_id
    assert terminal.tenant_id == request.tenant_id
    assert terminal.task_type == request.task_type
    assert terminal.operational_outcome.value == "boundary_failure"
    assert terminal.content_verdict.value == "not_applicable"
    assert terminal.terminal_failure_reason == "delegation_contract_unavailable"
    assert terminal.terminal_failure_cause is None
    assert f"errno={error_number}" in terminal.failure_reason
    assert terminal.quality_passed is False
    assert terminal.quality_score is None
    assert terminal.required_quality_bar is None
    assert terminal.attempts_count == 0
    assert terminal.escalation_history == ()
    assert terminal.content == ""
    assert terminal.model_used == "none"
    assert terminal.endpoint_url == "none"
    assert terminal.total_tokens == 0
    assert handler.workflows[request.correlation_id].state == EnumDelegationState.FAILED
    # A replay cannot turn the failed request into a routing attempt.
    assert await handler.handle(request) == []


async def test_valid_contract_still_produces_routing_intent() -> None:
    handler = HandlerDelegationWorkflow(workflows={})
    request = _request()
    events = await handler.handle(request)
    assert len(events) == 1
    assert type(events[0]).__name__ == "ModelRoutingIntent"
    assert (
        handler.workflows[request.correlation_id].state == EnumDelegationState.RECEIVED
    )


async def test_programming_errors_are_not_relabelled_as_contract_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise AssertionError("a programming error")

    monkeypatch.setattr(workflow_module, "_response_contract_metadata", broken)
    handler = HandlerDelegationWorkflow(workflows={})
    with pytest.raises(AssertionError, match="a programming error"):
        await handler.handle(_request())


@pytest.mark.skipif(sys.platform != "linux", reason="isolated Linux descriptor probe")
def test_real_descriptor_exhaustion_still_constructs_failure_terminal() -> None:
    # Exhaust descriptors only in a child. Import and warm the handler first,
    # then fail a genuine open(), including the terminal's optional hash reads.
    probe = r"""
import asyncio
import json
import os
import resource
from datetime import UTC, datetime
from uuid import uuid4
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import HandlerDelegationWorkflow
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import ModelDelegationRequest
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import ModelDelegationFailed
handler = HandlerDelegationWorkflow(workflows={})
def request():
    return ModelDelegationRequest(correlation_id=uuid4(), task_type="document",
        prompt="Write a PR body.", emitted_at=datetime.now(UTC), tenant_id="probe")
async def run():
    await handler.handle(request())
    failed_request = request()
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(64, soft), hard))
    descriptors = []
    try:
        while True:
            try:
                descriptors.append(os.open(os.devnull, os.O_RDONLY))
            except OSError:
                break
        events = await handler.handle(failed_request)
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    terminal = next(event for event in events if isinstance(event, ModelDelegationFailed))
    print(json.dumps({"reason": terminal.terminal_failure_reason,
        "attempts": terminal.attempts_count, "tenant": terminal.tenant_id,
        "failed_state": handler.workflows[failed_request.correlation_id].state.value}))
asyncio.run(run())
"""
    done = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {
        "reason": "delegation_contract_unavailable",
        "attempts": 0,
        "tenant": "probe",
        "failed_state": "FAILED",
    }
