# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19432: the shipped routing contract walks flash, then glm-5.3, through the real port.

``test_omn19432_flash_first_glm_ladder`` pins the ladder as the resolver's answer over
the shipped ``routing_tiers.yaml``. This file pins it as what the in-process dispatch
port DOES with that contract: flash is called first, and a flash capacity refusal
(HTTP 429, z.ai code 1302) or a flash answer the quality gate refuses moves the next
attempt to glm-5.3, ahead of every Gemini rung. Only the outbound HTTP call is faked;
the routing authority, the task-class contract and the quality gate are the real ones.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.inference import provider_quota_state
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG

pytestmark = pytest.mark.unit

_FLASH = "cloud-glm"
_STRONG = "cloud-glm-5-3"
_GEMINI = ("cloud-gemini-pro", "cloud-gemini-flash")
_GOOD = "### ANSWER\nThe two caching strategies trade freshness for hit rate."


@pytest.fixture(autouse=True)
def _real_contract_with_a_glm_key(
    monkeypatch: pytest.MonkeyPatch, register_local_secret: Callable[[str, str], None]
) -> Iterator[None]:
    for name in (
        "DELEGATION_ROUTING_TIERS_PATH",
        "BIFROST_CONTRACT_PATH",
        "BIFROST_OVERLAY_PATH",
        "TASK_CLASS_CONTRACT_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    register_local_secret("llm.glm.api_key", "test-key-not-a-secret")
    provider_quota_state.clear_provider_quota_state()
    routing._config = None
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()
    yield
    provider_quota_state.clear_provider_quota_state()
    routing._config = None
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()


class _ScriptedGlm:
    """Answer per backend.

    The flash rung follows ``flash_says``; every other rung answers well.
    """

    def __init__(self, flash_says: str) -> None:
        self.flash_says = flash_says
        self.calls: list[str] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request.provider)
        if request.provider == _FLASH and self.flash_says == "capacity":
            return ModelLlmDelegationCallResult(
                request_id=request.request_id,
                success=False,
                failure_class=EnumDelegationFailureClass.RATE_LIMITED,
                error_message='HTTP 429 {"error":{"code":"1302"}}',
            )
        if request.provider == _FLASH and self.flash_says == "empty":
            return ModelLlmDelegationCallResult(
                request_id=request.request_id, success=True, content=""
            )
        return ModelLlmDelegationCallResult(
            request_id=request.request_id, success=True, content=_GOOD
        )


def _dispatch(effect: _ScriptedGlm, tmp_path: Path) -> dict[str, Any]:
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    return asyncio.run(
        port.dispatch(
            prompt="Summarize in one sentence: the two caching strategies trade freshness for hit rate.",
            task_type="summarization",
            correlation_id=uuid4(),
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=240,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            # The house tenant may use the house GLM key; an un-attributed local run
            # is a customer install and is refused it (customer key terminus).
            tenant_id=HOUSE_TENANT_SLUG,
            # Start on the flash rung: with no local overlay in a unit test the local
            # tier has no endpoint, and what is under test is the hop AFTER flash.
            backend_id=_FLASH,
        )
    )


def test_a_flash_answer_that_passes_the_gate_is_accepted_without_touching_glm_5_3(
    tmp_path: Path,
) -> None:
    effect = _ScriptedGlm("good")
    result = _dispatch(effect, tmp_path)

    assert result["status"] == "completed", result
    assert effect.calls == [_FLASH]


def test_a_flash_capacity_refusal_moves_to_glm_5_3_and_is_recorded_as_capacity(
    tmp_path: Path,
) -> None:
    effect = _ScriptedGlm("capacity")
    result = _dispatch(effect, tmp_path)

    assert effect.calls == [_FLASH, _STRONG], effect.calls
    assert not set(effect.calls) & set(_GEMINI)
    assert result["status"] == "completed", result
    flash_attempt = next(a for a in result["attempts"] if a["backend_id"] == _FLASH)
    assert (
        flash_attempt["failure_class"] == EnumDelegationFailureClass.RATE_LIMITED.value
    )
    # Capacity, not quality: the call never returned an answer, so no quality verdict
    # exists and the record says the provider call failed.
    assert flash_attempt["quality_gate_passed"] is False
    assert flash_attempt["quality_score"] is None
    assert flash_attempt["acceptance_reason"] == "provider_call_failed"


def test_a_flash_answer_the_gate_refuses_moves_to_glm_5_3(tmp_path: Path) -> None:
    effect = _ScriptedGlm("empty")
    result = _dispatch(effect, tmp_path)

    assert effect.calls == [_FLASH, _STRONG], effect.calls
    assert not set(effect.calls) & set(_GEMINI)
    assert result["status"] == "completed", result
