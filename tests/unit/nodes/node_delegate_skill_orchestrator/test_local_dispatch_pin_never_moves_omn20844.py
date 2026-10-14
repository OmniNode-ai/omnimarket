# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-process port runs a pinned delegation on the pin or fails typed (OMN-20844).

``LocalDelegationDispatchPort`` (``onex delegate --bus inmemory --backend-id``)
pinned only the INITIAL attempt: a transport failure on the pin took a
same-tier sibling hop or an escalation, and a gate refusal did the same, so
another backend answered under the pin's name. The bus path holds the same
rule in ``test_pinned_backend_never_moves_omn20844.py``.

The fixture is the OMN-13640 sibling fixture: ``cheap_cloud`` declares
``cloud-primary`` and an answering ``cloud-sibling``, so every move the
unpinned run makes is available to the pinned one too.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from tests.unit.nodes.node_delegate_skill_orchestrator.test_local_dispatch_sibling_backend_omn13640 import (
    _fixture_env,
    _port,
    _ScriptedEffect,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("_fixture_env")]

__all__ = ["_fixture_env"]

_PIN = "cloud-primary"


def _dispatch(
    port: LocalDelegationDispatchPort, *, backend_id: str | None
) -> dict[str, Any]:
    return asyncio.run(
        port.dispatch(
            prompt="explain the tradeoff between the two caching strategies",
            task_type="research",
            correlation_id=uuid4(),
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=240,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
            backend_id=backend_id,
        )
    )


class _EmptyFromFailing(_ScriptedEffect):
    """Answer with an empty body from the failing backends: the gate refuses it."""

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append((request.provider, request.model_id))
        content = "" if request.provider in self.fail_providers else self.answer
        return ModelLlmDelegationCallResult(
            request_id=request.request_id, success=True, content=content
        )


def test_unpinned_failure_on_primary_moves_to_the_sibling(tmp_path: Path) -> None:
    """Positive control: without a pin the run leaves ``cloud-primary``."""
    effect = _ScriptedEffect(fail_providers=frozenset({"local-x", _PIN}))

    _dispatch(_port(effect, tmp_path), backend_id=None)

    assert "cloud-sibling" in [provider for provider, _ in effect.calls]


def test_pinned_transport_failure_fails_typed_on_the_pin(tmp_path: Path) -> None:
    effect = _ScriptedEffect(fail_providers=frozenset({_PIN}))

    result = _dispatch(_port(effect, tmp_path), backend_id=_PIN)

    assert [provider for provider, _ in effect.calls] == [_PIN]
    assert result["status"] == "failed"
    assert result["terminal_failure_reason"] == "pinned_backend_failed"
    assert str(result["error_message"]).startswith("pinned_backend_failed")


def test_pinned_gate_refusal_fails_typed_on_the_pin(tmp_path: Path) -> None:
    effect = _EmptyFromFailing(fail_providers=frozenset({_PIN}))

    result = _dispatch(_port(effect, tmp_path), backend_id=_PIN)

    assert {provider for provider, _ in effect.calls} == {_PIN}
    assert result["status"] == "failed"
    assert result["terminal_failure_reason"] == "pinned_backend_failed"
