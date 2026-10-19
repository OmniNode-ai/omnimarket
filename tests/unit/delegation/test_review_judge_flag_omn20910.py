# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review class takes the independent judge behind a contract flag (OMN-20910).

The answer gate (OMN-20489) already refuses a text answer its independent judge
rejects, but nothing armed it for ``review``: the class gate accepted 60 of 60
Qwen3.8-27B reviews at score 1.0 and 35 of them were inadequate. The flag is the
``independent_judge.armed_task_classes`` list in
``configs/delegation_independent_judge.v1.yaml``; it ships empty, so the gate keeps the contract verdict until a
measurement turns the class on.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_routing_answer_judge as judge_port,
)
from omnimarket.nodes.node_delegation_answer_gate_compute.handlers import (
    judge_selection,
)
from omnimarket.nodes.node_delegation_answer_gate_compute.handlers.judge_selection import (
    independent_judge_armed,
    independent_judge_config,
    parse_judge_reply,
    render_judge_prompt,
    select_independent_judge,
)
from omnimarket.nodes.node_delegation_answer_gate_compute.models.model_answer_gate_request import (
    EnumIndependentJudgeVerdict,
    ModelAnswerJudgeCall,
    ModelIndependentJudgeVerdict,
)

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[3]
_ROUTING_CONTRACT = _REPO / "src" / "omnimarket" / "configs" / "bifrost_delegation.yaml"

_DIFF = (
    "diff --git a/src/pkg/mod.py b/src/pkg/mod.py\n"
    "--- a/src/pkg/mod.py\n"
    "+++ b/src/pkg/mod.py\n"
    "@@ -10,3 +10,4 @@\n"
    "  10|    value = load()\n"
    "+ 11|    value = value or DEFAULT\n"
    "  12|    return value\n"
)
_REVIEW_PROMPT = "Review this diff and cite file:line for each finding.\n\n" + _DIFF
_REVIEW_ANSWER = (
    "### ANSWER\n"
    "- `src/pkg/mod.py:11` falls back to DEFAULT when `load()` returns a falsy "
    "value, which also replaces a legitimate empty value."
)


def _routing_backends() -> list[dict[str, Any]]:
    return list(yaml.safe_load(_ROUTING_CONTRACT.read_text())["backends"])


def _arm(monkeypatch: pytest.MonkeyPatch, classes: list[str]) -> None:
    config = dict(independent_judge_config())
    config["independent_judge"] = {"armed_task_classes": classes}
    monkeypatch.setattr(judge_selection, "independent_judge_config", lambda: config)


# --- the flag ships off --------------------------------------------------------


def test_the_flag_ships_with_no_class_armed() -> None:
    assert independent_judge_config()["independent_judge"]["armed_task_classes"] == []
    assert independent_judge_armed("review") is False


def test_a_class_named_in_the_flag_is_armed(monkeypatch: pytest.MonkeyPatch) -> None:
    _arm(monkeypatch, ["review"])
    assert independent_judge_armed("review") is True
    assert independent_judge_armed("summarization") is False


# --- the review judge prompt and its reply -------------------------------------


def test_review_judge_prompt_asks_for_a_verdict_line_over_the_diff() -> None:
    system, user = render_judge_prompt(
        instruction="", source=_REVIEW_PROMPT, answer=_REVIEW_ANSWER, task_type="review"
    )
    assert "VERDICT: ACCEPT" in system + user
    assert "UNSUPPORTED" in system + user
    assert "value = value or DEFAULT" in user
    assert "src/pkg/mod.py:11" in user


def test_other_classes_keep_the_json_judge_prompt() -> None:
    system, _ = render_judge_prompt(
        instruction="", source="s", answer="a", task_type="summarization"
    )
    assert '{"accept": true or false' in system


@pytest.mark.parametrize(
    ("reply", "verdict"),
    [
        ("Step 1.\n- Finding 1: SUPPORTED\nStep 2. NONE\nVERDICT: ACCEPT", "accept"),
        ("Step 1.\n- Finding 1: UNSUPPORTED\nStep 2. NONE\nVERDICT: REJECT", "reject"),
        ("**VERDICT: REJECT**", "reject"),
        ("verdict: accept", "accept"),
        # the last verdict line decides, not one quoted in the reasoning
        ("The rubric says VERDICT: ACCEPT only when none.\nVERDICT: REJECT", "reject"),
        ("no verdict at all", "unparseable"),
        ("VERDICT: MAYBE", "unparseable"),
        ("", "unparseable"),
    ],
)
def test_review_reply_is_read_from_the_last_verdict_line(
    reply: str, verdict: str
) -> None:
    parsed, _reason = parse_judge_reply(reply, task_type="review")
    assert parsed is EnumIndependentJudgeVerdict(verdict)


def test_json_reply_is_unchanged_for_other_classes() -> None:
    parsed, reason = parse_judge_reply(
        '{"accept": false, "reason": "wrong subject"}', task_type="summarization"
    )
    assert parsed is EnumIndependentJudgeVerdict.REJECT
    assert reason == "wrong subject"
    assert (
        parse_judge_reply("VERDICT: ACCEPT", task_type="summarization")[0]
        is EnumIndependentJudgeVerdict.UNPARSEABLE
    )


# --- the judge backend an ordinary call uses -----------------------------------


def test_ordinary_selection_skips_a_pin_only_backend_when_another_qualifies() -> None:
    judge = select_independent_judge("Qwen3.8-27B", _routing_backends())
    assert judge is not None
    assert not judge.get("explicit_pin_only")
    assert "judge_adequacy" in judge["capabilities"]


def test_a_pin_only_judge_is_the_last_resort() -> None:
    backends = [
        {
            "backend_id": "pinned",
            "model_name": "gemini-2.5-flash",
            "explicit_pin_only": True,
            "capabilities": ["judge_adequacy"],
        }
    ]
    selected = select_independent_judge("Qwen3.8-27B", backends)
    assert selected is not None
    assert selected["backend_id"] == "pinned"


# --- the production judge call --------------------------------------------------


class _FakeAdapter:
    def __init__(self, *, backend_id: str, reply: str, model_id: str) -> None:
        self.backend_id = backend_id
        self.reply = reply
        self.model_id = model_id
        self.calls: list[tuple[str, str]] = []

    def resolved_model_id(self) -> str:
        return self.model_id

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None = None,
        **_: object,
    ) -> str:
        self.calls.append((system_prompt, user_prompt))
        return self.reply


def _call(
    model_id: str = "nvidia/nemotron-3-super-120b-a12b:free",
) -> ModelAnswerJudgeCall:
    return ModelAnswerJudgeCall(
        backend_id="openrouter-nemotron-super",
        model_id=model_id,
        system_prompt="system",
        user_prompt="user",
    )


def _install(
    monkeypatch: pytest.MonkeyPatch, *, reply: str, resolved: str
) -> list[_FakeAdapter]:
    built: list[_FakeAdapter] = []

    def _factory(*, backend_id: str) -> _FakeAdapter:
        adapter = _FakeAdapter(backend_id=backend_id, reply=reply, model_id=resolved)
        built.append(adapter)
        return adapter

    monkeypatch.setattr(judge_port, "RoutingResolvedJudgeInferenceAdapter", _factory)
    return built


def test_routing_judge_posts_to_the_selected_backend_and_reads_the_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = _install(
        monkeypatch,
        reply="Step 1. fine\nVERDICT: REJECT",
        resolved="nvidia/nemotron-3-super-120b-a12b:free",
    )
    verdict = asyncio.run(judge_port.routing_answer_judge(_call(), task_type="review"))
    assert verdict.verdict is EnumIndependentJudgeVerdict.REJECT
    assert verdict.backend_id == "openrouter-nemotron-super"
    assert built[0].backend_id == "openrouter-nemotron-super"
    assert built[0].calls == [("system", "user")]


def test_routing_judge_refuses_a_reviewer_that_is_not_the_selected_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The adapter falls back to a local rung (Qwen) when the declared judge's
    # key is absent; that reviewer is the producer's own family, so it must not
    # be recorded as the cross-family judge.
    _install(monkeypatch, reply="VERDICT: ACCEPT", resolved="Qwen3.8-27B")
    with pytest.raises(RuntimeError, match="not the selected judge"):
        asyncio.run(judge_port.routing_answer_judge(_call(), task_type="review"))


# --- the dispatch path -----------------------------------------------------------


def _dispatch_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer_judge: Callable[[ModelAnswerJudgeCall], ModelIndependentJudgeVerdict] | None,
) -> dict[str, object]:
    from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
        port_local_delegation_dispatch as port_mod,
    )
    from omnimarket.nodes.node_delegation_orchestrator.handlers import (
        handler_delegation_workflow as hw,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect import (
        ModelLlmDelegationCallRequest,
        ModelLlmDelegationCallResult,
    )
    from omnimarket.routing.delegation_backend_resolution import (
        ModelResolvedDelegationBackend,
    )

    local_backend = ModelResolvedDelegationBackend(
        backend_id="local-heavy-reasoning",
        model_id="Qwen3.8-27B",
        endpoint_ref="https://local.example/v1/chat/completions",
        tier="local",
        max_tokens=4096,
        timeout_ms=30000,
    )
    monkeypatch.setattr(
        port_mod, "resolve_delegation_backend", lambda *_a, **_k: local_backend
    )
    monkeypatch.setattr(port_mod, "next_eligible_tier", lambda *_a, **_k: None)
    monkeypatch.setattr(port_mod, "tier_for_backend", lambda _backend_id: "local")
    monkeypatch.setattr(
        port_mod, "resolve_task_class_max_escalations", lambda _task_type: 0
    )
    monkeypatch.setattr(hw, "next_eligible_tier", lambda *_a, **_k: None)
    monkeypatch.setattr(hw, "tier_max_retries", lambda _tier: 0)
    monkeypatch.setattr(hw, "is_free_tier", lambda _tier: False)
    monkeypatch.setattr(port_mod, "load_bifrost_backends", _routing_backends)

    def _effect(request: ModelLlmDelegationCallRequest) -> ModelLlmDelegationCallResult:
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=_REVIEW_ANSWER,
            tokens_in=40,
            tokens_out=20,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )

    port = port_mod.LocalDelegationDispatchPort(
        effect_handler=_effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
        answer_judge=answer_judge,
    )
    return asyncio.run(
        port.dispatch(
            prompt=_REVIEW_PROMPT,
            task_type="review",
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
            named_tests=None,
        )
    )


@pytest.mark.usefixtures("stub_provider_quota_reader")
def test_review_with_the_flag_off_makes_no_judge_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _install(monkeypatch, reply="VERDICT: REJECT", resolved="x")
    result = _dispatch_review(tmp_path, monkeypatch, None)
    assert result["quality_gate_passed"] is True
    assert built == []


@pytest.mark.usefixtures("stub_provider_quota_reader")
def test_review_with_the_flag_on_is_refused_when_the_judge_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arm(monkeypatch, ["review"])
    built = _install(
        monkeypatch,
        reply="Step 1. UNSUPPORTED\nVERDICT: REJECT",
        resolved="nvidia/nemotron-3-super-120b-a12b:free",
    )
    result = _dispatch_review(tmp_path, monkeypatch, None)
    assert result["quality_gate_passed"] is False
    # a refusal climbs the ladder, so the same answer may be judged again
    assert built
    assert "value = value or DEFAULT" in built[0].calls[0][1]


@pytest.mark.usefixtures("stub_provider_quota_reader")
def test_review_with_the_flag_on_is_accepted_when_the_judge_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arm(monkeypatch, ["review"])
    _install(
        monkeypatch,
        reply="Step 1. SUPPORTED\nStep 2. NONE\nVERDICT: ACCEPT",
        resolved="nvidia/nemotron-3-super-120b-a12b:free",
    )
    result = _dispatch_review(tmp_path, monkeypatch, None)
    assert result["quality_gate_passed"] is True


@pytest.mark.usefixtures("stub_provider_quota_reader")
def test_review_with_the_flag_on_is_refused_when_no_cross_family_reviewer_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arm(monkeypatch, ["review"])
    _install(monkeypatch, reply="VERDICT: ACCEPT", resolved="Qwen3.8-27B")
    result = _dispatch_review(tmp_path, monkeypatch, None)
    assert result["quality_gate_passed"] is False


@pytest.mark.usefixtures("stub_provider_quota_reader")
def test_an_injected_judge_wins_over_the_default_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arm(monkeypatch, ["review"])
    built = _install(monkeypatch, reply="VERDICT: REJECT", resolved="x")
    calls: list[ModelAnswerJudgeCall] = []

    def _judge(call: ModelAnswerJudgeCall) -> ModelIndependentJudgeVerdict:
        calls.append(call)
        return ModelIndependentJudgeVerdict(
            backend_id=call.backend_id,
            model_id=call.model_id,
            verdict=EnumIndependentJudgeVerdict.ACCEPT,
        )

    result = _dispatch_review(tmp_path, monkeypatch, _judge)
    assert result["quality_gate_passed"] is True
    assert len(calls) == 1
    assert built == []
