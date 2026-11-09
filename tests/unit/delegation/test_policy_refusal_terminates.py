# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23216: a refusal on policy grounds ends the run; a 503 on the rung still moves.

A local rung that refuses a secret or exfiltration request has given the right
answer, and no costlier rung improves it. The declared transition lists
(``task_class_contracts.v1.yaml``) now name that cause, ``policy_refusal``, in
``terminate_on`` for every task class, and both delegation paths read it from
there. The ladder, the rungs and the provider are the stubs of the OMN-22212
fixture: a free local rung with a re-draft budget of two and a metered cloud
rung above it. Fakes only, so no model is called and nothing is spent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.inference.task_class_authority import load_task_class_authority
from omnimarket.models.delegation.model_escalation_transitions import (
    EnumEscalationCause,
    EnumTransitionKind,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from tests.unit.delegation.test_secret_request_refusal_terminal import (
    CLOUD_ANSWER,
    ORDINARY_PROMPT,
    SECRET_PROMPT,
    _AnswersByProvider,
    _dispatch,
)
from tests.unit.delegation.test_secret_request_refusal_terminal import (
    _fixture_env as _fixture_env,
)

pytestmark = pytest.mark.unit


class _Rung503(_AnswersByProvider):
    """The local rung answers 503 on every call; the cloud rung answers."""

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request.provider)
        if request.provider == "local-chat":
            return ModelLlmDelegationCallResult(
                request_id=request.request_id,
                success=False,
                error_message="provider http 503: upstream unavailable",
                http_status=503,
            )
        return ModelLlmDelegationCallResult(
            request_id=request.request_id, success=True, content=CLOUD_ANSWER
        )


def test_the_contract_lists_policy_refusal_as_terminal_for_every_class() -> None:
    """The cause is declared once per class, in terminate_on."""
    authority = load_task_class_authority()
    assert authority.task_classes
    for name, entry in authority.task_classes.items():
        assert entry.transitions is not None, name
        assert (
            entry.transitions.kind_of(EnumEscalationCause.POLICY_REFUSAL)
            is EnumTransitionKind.TERMINATE
        ), name


@pytest.mark.usefixtures("_fixture_env")
def test_a_policy_refusal_on_rung_one_makes_no_later_attempt(tmp_path: Path) -> None:
    """AC1, first half: one local call, no cloud call, the cause recorded as policy_refusal."""
    effect = _AnswersByProvider()

    result: dict[str, Any] = _dispatch(effect, tmp_path, SECRET_PROMPT)

    assert effect.calls == ["local-chat"]
    assert result["status"] == "failed"
    assert result["escalation_count"] == 0
    (attempt,) = result["attempts"]
    assert attempt["transition"] == {
        "kind": "terminate",
        "cause": EnumEscalationCause.POLICY_REFUSAL.value,
        "move_kind": None,
    }
    assert "I cannot share API keys" in result["content"]


@pytest.mark.usefixtures("_fixture_env")
def test_a_503_on_rung_one_still_moves_even_for_a_secret_request(
    tmp_path: Path,
) -> None:
    """AC1, second half: an unavailable rung is not a policy refusal, so the run moves."""
    effect = _Rung503()

    result: dict[str, Any] = _dispatch(effect, tmp_path, SECRET_PROMPT)

    assert effect.calls == ["local-chat", "cloud-chat"]
    (local,) = [a for a in result["attempts"] if a["tier"] == "local"]
    assert local["transition"] == {
        "kind": "move",
        "cause": EnumEscalationCause.RETRY_BUDGET_SPENT.value,
        "move_kind": "tier_climb",
    }


@pytest.mark.usefixtures("_fixture_env")
def test_control_without_the_policy_cause_the_refusal_climbs_to_the_cloud_rung(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing control: classify the refusal as an ordinary quality refusal and it climbs."""
    monkeypatch.setattr(
        port_mod,
        "local_quality_cause",
        lambda **_kw: EnumEscalationCause.QUALITY_GATE_REFUSED,
    )
    effect = _AnswersByProvider()

    _dispatch(effect, tmp_path, SECRET_PROMPT)

    assert "cloud-chat" in effect.calls, (
        f"with the cause removed the secret request was expected to climb: {effect.calls}"
    )


@pytest.mark.usefixtures("_fixture_env")
def test_an_ordinary_refusal_is_not_a_policy_refusal(tmp_path: Path) -> None:
    """The cause marks a refused secret request only; the same refusal elsewhere climbs."""
    effect = _AnswersByProvider()

    result: dict[str, Any] = _dispatch(effect, tmp_path, ORDINARY_PROMPT)

    assert "cloud-chat" in effect.calls, effect.calls
    assert all(
        a["transition"]["cause"] != EnumEscalationCause.POLICY_REFUSAL.value
        for a in result["attempts"]
    )
