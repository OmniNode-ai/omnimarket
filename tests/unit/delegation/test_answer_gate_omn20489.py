# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A delegated answer is accepted only after its gate (OMN-20489).

Text class: the class's contract checks pass AND an independent judge, from a
different model family than the producer, accepts the content. Code class: the
contract checks pass AND the tests the task names ran against the answer and
passed. A rejection recommends escalation the way a contract failure does.

The text cases replay 120 labelled summarization answers: a careful reading of
each answer against its source, the contract grader's verdict and a judge's
verdict, all recorded before this gate existed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml

from omnimarket.models.delegation.wire.model_named_test_run import ModelNamedTestRun
from omnimarket.models.delegation.wire.model_quality_gate import ModelQualityGateResult
from omnimarket.nodes.node_delegation_answer_gate_compute.handlers.handler_delegation_answer_gate import (
    JUDGE_NOT_INDEPENDENT_PREFIX,
    JUDGE_REJECTED_PREFIX,
    JUDGE_UNAVAILABLE_PREFIX,
    TESTS_FAILED_PREFIX,
    HandlerDelegationAnswerGate,
    gate_as_quality_gate_result,
    resolve_answer_gate_kind,
)
from omnimarket.nodes.node_delegation_answer_gate_compute.handlers.judge_selection import (
    model_family,
    parse_judge_reply,
    render_judge_prompt,
    select_independent_judge,
)
from omnimarket.nodes.node_delegation_answer_gate_compute.models.model_answer_gate_request import (
    EnumAnswerGateKind,
    EnumIndependentJudgeVerdict,
    ModelAnswerGateRequest,
    ModelAnswerJudgeCall,
    ModelIndependentJudgeVerdict,
)

type _AnswerJudge = Callable[[ModelAnswerJudgeCall], ModelIndependentJudgeVerdict]

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[3]
_FIXTURE = (
    _REPO / "tests" / "fixtures" / "delegation_answer_gate" / "labelled_summaries.json"
)
_ROUTING_CONTRACT = _REPO / "src" / "omnimarket" / "configs" / "bifrost_delegation.yaml"
_CORRELATION = UUID("00000000-0000-4000-8000-000000020489")
_JUDGE_BACKEND = "cloud-gemini-judge"
_JUDGE_MODEL = "gemini-2.5-flash"


def _labelled_rows() -> list[dict[str, object]]:
    data = json.loads(_FIXTURE.read_text())
    return [dict(zip(data["columns"], row, strict=True)) for row in data["rows"]]


def _contract(passed: bool, reasons: tuple[str, ...] = ()) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=_CORRELATION,
        passed=passed,
        fail_category="pass" if passed else "fail_heuristic",
        quality_score=1.0 if passed else 0.0,
        failure_reasons=() if passed else (reasons or ("WEAK_OUTPUT: contract",)),
        fallback_recommended=not passed,
    )


def _judge(
    verdict: EnumIndependentJudgeVerdict,
    *,
    model_id: str = _JUDGE_MODEL,
    backend_id: str = _JUDGE_BACKEND,
) -> ModelIndependentJudgeVerdict:
    return ModelIndependentJudgeVerdict(
        backend_id=backend_id,
        model_id=model_id,
        verdict=verdict,
        reason="recorded",
        tokens_in=2000,
        tokens_out=60,
    )


def _text_request(
    *,
    contract_passed: bool,
    judge: ModelIndependentJudgeVerdict | None,
    producer: str = "Qwen3.8-27B",
    task_type: str = "summarization",
) -> ModelAnswerGateRequest:
    return ModelAnswerGateRequest(
        correlation_id=_CORRELATION,
        task_type=task_type,
        gate_kind=EnumAnswerGateKind.TEXT,
        producer_model_id=producer,
        contract_gate=_contract(contract_passed),
        judge=judge,
    )


_GATE = HandlerDelegationAnswerGate()


# --- which classes take which gate -------------------------------------------


@pytest.mark.parametrize(
    "task_type",
    [
        "summarization",
        "document",
        "documentation",
        "review",
        "code_review",
        "research",
        "reasoning",
        "complex_reasoning",
        "planning",
        "escalation",
    ],
)
def test_prose_classes_take_the_text_gate(task_type: str) -> None:
    assert resolve_answer_gate_kind(task_type) is EnumAnswerGateKind.TEXT


@pytest.mark.parametrize(
    "task_type", ["code_generation", "refactor", "test", "validator_generation"]
)
def test_execution_classes_take_the_code_gate(task_type: str) -> None:
    assert resolve_answer_gate_kind(task_type) is EnumAnswerGateKind.CODE


@pytest.mark.parametrize("task_type", ["agent_delegation", "no_such_class"])
def test_other_classes_keep_their_contract_verdict(task_type: str) -> None:
    assert resolve_answer_gate_kind(task_type) is EnumAnswerGateKind.CONTRACT_ONLY


# --- text gate over the labelled rows -----------------------------------------


def test_fixture_holds_the_120_labelled_rows() -> None:
    rows = _labelled_rows()
    assert len(rows) == 120
    assert sum(1 for r in rows if not r["careful_accept"]) == 19


def test_text_answer_is_accepted_only_when_contract_and_judge_both_accept() -> None:
    for row in _labelled_rows():
        verdict = (
            EnumIndependentJudgeVerdict.ACCEPT
            if row["judge_accept"]
            else EnumIndependentJudgeVerdict.REJECT
        )
        result = _GATE.handle(
            _text_request(
                contract_passed=bool(row["grader_accept"]),
                judge=_judge(verdict),
                producer=str(row["producer_model"]),
            )
        )
        assert result.accepted is (
            bool(row["grader_accept"]) and bool(row["judge_accept"])
        ), row
        assert result.contract_passed is bool(row["grader_accept"])
        assert result.judge is not None
        assert result.judge.verdict is verdict
        if not result.accepted:
            assert result.fallback_recommended is True


def test_judge_veto_stops_answers_the_contract_grader_let_through() -> None:
    """Of the 19 answers a careful reading rejects, the contract grader passed 17;
    with the judge's recorded verdict beside it, the gate passes 7."""
    bad = [r for r in _labelled_rows() if not r["careful_accept"]]
    grader_passed = sum(1 for r in bad if r["grader_accept"])
    gate_passed = 0
    for row in bad:
        verdict = (
            EnumIndependentJudgeVerdict.ACCEPT
            if row["judge_accept"]
            else EnumIndependentJudgeVerdict.REJECT
        )
        if _GATE.handle(
            _text_request(
                contract_passed=bool(row["grader_accept"]),
                judge=_judge(verdict),
                producer=str(row["producer_model"]),
            )
        ).accepted:
            gate_passed += 1
    assert (grader_passed, gate_passed) == (17, 7)


def test_grader_lenient_fabrication_is_rejected_with_the_judge_reason() -> None:
    row = next(
        r
        for r in _labelled_rows()
        if r["disagreement_class"] == "grader_too_lenient"
        and r["judge_failure_class"] == "fabrication"
    )
    result = _GATE.handle(
        _text_request(
            contract_passed=True,
            judge=_judge(EnumIndependentJudgeVerdict.REJECT),
            producer=str(row["producer_model"]),
        )
    )
    assert result.accepted is False
    assert result.contract_passed is True
    assert result.rejection_reasons[0].startswith(JUDGE_REJECTED_PREFIX)


def test_contract_failure_is_not_lifted_by_a_judge_accept() -> None:
    result = _GATE.handle(
        _text_request(
            contract_passed=False, judge=_judge(EnumIndependentJudgeVerdict.ACCEPT)
        )
    )
    assert result.accepted is False
    assert result.rejection_reasons == ("WEAK_OUTPUT: contract",)


@pytest.mark.parametrize(
    "verdict",
    [EnumIndependentJudgeVerdict.UNAVAILABLE, EnumIndependentJudgeVerdict.UNPARSEABLE],
)
def test_text_answer_without_a_judge_verdict_is_refused(
    verdict: EnumIndependentJudgeVerdict,
) -> None:
    result = _GATE.handle(_text_request(contract_passed=True, judge=_judge(verdict)))
    assert result.accepted is False
    assert result.fallback_recommended is True
    assert result.rejection_reasons[0].startswith(JUDGE_UNAVAILABLE_PREFIX)


def test_text_answer_with_no_judge_at_all_is_refused() -> None:
    result = _GATE.handle(_text_request(contract_passed=True, judge=None))
    assert result.accepted is False
    assert result.rejection_reasons[0].startswith(JUDGE_UNAVAILABLE_PREFIX)


@pytest.mark.parametrize(
    ("producer", "judge_model"),
    [
        ("Qwen3.8-27B", "qwen3-coder-30b"),
        ("gemini-2.5-flash-lite", "gemini-2.5-flash"),
        ("Qwen3.8-27B", "some-unknown-model"),
    ],
)
def test_a_judge_of_the_same_or_unknown_family_does_not_count(
    producer: str, judge_model: str
) -> None:
    result = _GATE.handle(
        _text_request(
            contract_passed=True,
            judge=_judge(EnumIndependentJudgeVerdict.ACCEPT, model_id=judge_model),
            producer=producer,
        )
    )
    assert result.accepted is False
    assert result.judge_independent is False
    assert result.rejection_reasons[0].startswith(JUDGE_NOT_INDEPENDENT_PREFIX)


def test_receipt_records_both_verdicts_and_families() -> None:
    result = _GATE.handle(
        _text_request(
            contract_passed=True, judge=_judge(EnumIndependentJudgeVerdict.ACCEPT)
        )
    )
    assert result.accepted is True
    assert result.contract_passed is True
    assert result.judge is not None
    assert result.judge.backend_id == _JUDGE_BACKEND
    assert (result.producer_family, result.judge_family) == ("qwen", "gemini")
    assert result.judge_independent is True
    assert result.tests_passed is None


# --- rejection escalates the way a contract failure does ---------------------


def test_rejection_becomes_a_failed_quality_gate_that_recommends_fallback() -> None:
    contract = _contract(True)
    gate = _GATE.handle(
        _text_request(
            contract_passed=True, judge=_judge(EnumIndependentJudgeVerdict.REJECT)
        )
    )
    folded = gate_as_quality_gate_result(contract, gate)
    assert folded.passed is False
    assert folded.fallback_recommended is True
    assert folded.fail_category == "fail_heuristic"
    assert folded.failure_reasons == gate.rejection_reasons
    judged = folded.rule_evaluations[-1]
    assert judged.rule == f"independent_judge:{_JUDGE_BACKEND}"
    assert (judged.passed, judged.enforcement) == (False, "blocking")
    assert judged.detail == gate.rejection_reasons[0]


def test_acceptance_keeps_the_pass_and_records_the_judge_verdict() -> None:
    contract = _contract(True)
    gate = _GATE.handle(
        _text_request(
            contract_passed=True, judge=_judge(EnumIndependentJudgeVerdict.ACCEPT)
        )
    )
    folded = gate_as_quality_gate_result(contract, gate)
    assert folded.passed is True
    assert folded.failure_reasons == ()
    assert [(e.rule, e.passed) for e in folded.rule_evaluations] == [
        (f"independent_judge:{_JUDGE_BACKEND}", True)
    ]


def test_contract_failure_is_returned_as_the_contract_said() -> None:
    contract = _contract(False)
    gate = _GATE.handle(
        _text_request(
            contract_passed=False, judge=_judge(EnumIndependentJudgeVerdict.ACCEPT)
        )
    )
    assert gate_as_quality_gate_result(contract, gate) is contract


def test_contract_only_class_keeps_its_contract_verdict() -> None:
    for passed in (True, False):
        result = _GATE.handle(
            ModelAnswerGateRequest(
                correlation_id=_CORRELATION,
                task_type="agent_delegation",
                gate_kind=EnumAnswerGateKind.CONTRACT_ONLY,
                producer_model_id="Qwen3.8-27B",
                contract_gate=_contract(passed),
            )
        )
        assert result.accepted is passed
        assert result.judge_independent is None


# --- the judge comes from the routing contract --------------------------------


def _routing_backends() -> list[dict[str, object]]:
    return list(yaml.safe_load(_ROUTING_CONTRACT.read_text())["backends"])


@pytest.mark.parametrize(
    "producer",
    ["Qwen3.8-27B", "gemini-2.5-flash-lite", "nvidia/nemotron-3-super-120b-a12b:free"],
)
def test_judge_is_a_routing_contract_judge_backend_of_another_family(
    producer: str,
) -> None:
    backends = _routing_backends()
    judge = select_independent_judge(producer, backends)
    assert judge is not None
    assert "judge_adequacy" in judge["capabilities"]
    assert judge in backends
    judge_family = model_family(str(judge["model_name"]))
    assert judge_family is not None
    assert judge_family != model_family(producer)


def test_judge_selection_reads_capabilities_not_a_fixed_backend() -> None:
    backends = [
        {
            "backend_id": "a",
            "model_name": "Qwen3-235B",
            "capabilities": ["judge_adequacy"],
        },
        {"backend_id": "b", "model_name": "glm-5.3", "capabilities": ["reasoning"]},
        {
            "backend_id": "c",
            "model_name": "deepseek-v4",
            "capabilities": ["judge_adequacy"],
        },
    ]
    selected = select_independent_judge("Qwen3.8-27B", backends)
    assert selected is not None
    assert selected["backend_id"] == "c"
    assert select_independent_judge("deepseek-v4", backends[2:]) is None


# --- the judge prompt reads source and answer, and counts nothing ------------


def test_judge_prompt_carries_source_and_answer_and_forbids_counting() -> None:
    system, user = render_judge_prompt(
        instruction="Summarize in at most 2 sentences.",
        source="The release train opened version 0.4.314.",
        answer="The train opened 0.4.314.",
    )
    assert "The release train opened version 0.4.314." in user
    assert "The train opened 0.4.314." in user
    assert "Summarize in at most 2 sentences." in user
    assert "Do not count sentences or words" in system


def test_judge_prompt_without_instruction_and_with_braces_in_the_source() -> None:
    _, user = render_judge_prompt(
        instruction="",
        source="Summarize: the template {answer} stays literal.",
        answer="A short answer.",
    )
    assert "INSTRUCTION" not in user
    assert "the template {answer} stays literal." in user
    assert user.count("A short answer.") == 1


@pytest.mark.parametrize(
    ("reply", "verdict"),
    [
        (
            '{"accept": true, "reason": "all supported"}',
            EnumIndependentJudgeVerdict.ACCEPT,
        ),
        (
            '```json\n{"accept": false, "reason": "wrong subject"}\n```',
            EnumIndependentJudgeVerdict.REJECT,
        ),
        (
            'Verdict below.\n{"accept": false, "reason": "x"}',
            EnumIndependentJudgeVerdict.REJECT,
        ),
        ("looks fine to me", EnumIndependentJudgeVerdict.UNPARSEABLE),
        ('{"accept": "yes"}', EnumIndependentJudgeVerdict.UNPARSEABLE),
        ("", EnumIndependentJudgeVerdict.UNPARSEABLE),
    ],
)
def test_judge_reply_parses_to_a_verdict(
    reply: str, verdict: EnumIndependentJudgeVerdict
) -> None:
    assert parse_judge_reply(reply)[0] is verdict


# --- code gate: the answer's own named tests, run in a workspace copy ---------

_ANSWER_PATH = "pkg/slugify.py"
_TEST_ID = "tests/test_slugify.py::test_slugify"
_PASSING_ANSWER = (
    "def slugify(text: str) -> str:\n    return '-'.join(text.lower().split())\n"
)
_FAILING_ANSWER = "def slugify(text: str) -> str:\n    return text\n"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "tests").mkdir()
    (root / "tests" / "test_slugify.py").write_text(
        "from pkg.slugify import slugify\n\n\n"
        "def test_slugify() -> None:\n"
        "    assert slugify('Hello  Big World') == 'hello-big-world'\n"
    )
    return root


def _run_named_tests(workspace: Path, answer: str, tmp_path: Path) -> ModelNamedTestRun:
    sandbox = tmp_path / f"sandbox-{uuid4().hex}"
    shutil.copytree(workspace, sandbox)
    (sandbox / _ANSWER_PATH).write_text(answer)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", _TEST_ID],
        cwd=sandbox,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return ModelNamedTestRun(
        test_ids=(_TEST_ID,),
        failed_test_ids=() if proc.returncode == 0 else (_TEST_ID,),
        exit_code=proc.returncode,
    )


def _code_request(run: ModelNamedTestRun | None) -> ModelAnswerGateRequest:
    return ModelAnswerGateRequest(
        correlation_id=_CORRELATION,
        task_type="code_generation",
        gate_kind=EnumAnswerGateKind.CODE,
        producer_model_id="Qwen3.8-27B",
        contract_gate=_contract(True),
        named_test_run=run,
    )


def test_code_answer_whose_named_tests_pass_is_accepted(
    workspace: Path, tmp_path: Path
) -> None:
    run = _run_named_tests(workspace, _PASSING_ANSWER, tmp_path)
    assert run.passed
    result = _GATE.handle(_code_request(run))
    assert result.accepted is True
    assert result.tests_passed is True
    assert result.judge is None


def test_code_answer_whose_named_tests_fail_is_rejected_and_escalates(
    workspace: Path, tmp_path: Path
) -> None:
    run = _run_named_tests(workspace, _FAILING_ANSWER, tmp_path)
    assert not run.passed
    result = _GATE.handle(_code_request(run))
    assert result.accepted is False
    assert result.tests_passed is False
    assert result.fallback_recommended is True
    assert result.rejection_reasons[0].startswith(TESTS_FAILED_PREFIX)
    assert _TEST_ID in result.rejection_reasons[0]


def test_code_answer_with_named_tests_but_no_run_is_refused() -> None:
    request = _code_request(None).model_copy(update={"named_tests_requested": True})
    result = _GATE.handle(request)
    assert result.accepted is False
    assert result.tests_passed is False
    assert result.rejection_reasons[0].startswith("INSUFFICIENT")


# --- the in-process dispatch applies the gate when a judge is wired ----------

_SUMMARY_PROMPT = (
    "Summarize the following text in one sentence.\n\n"
    "The build cache was cleared on Monday and the nightly job passed afterwards."
)
_SUMMARY = (
    "The build cache was cleared on Monday and the nightly job passed afterwards."
)


def _dispatch_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer_judge: _AnswerJudge | None,
) -> dict[str, object]:
    import asyncio
    from decimal import Decimal

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
            content=f"### ANSWER\n{_SUMMARY}",
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
            prompt=_SUMMARY_PROMPT,
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
            tenant_id=None,
            named_tests=None,
        )
    )


def _recording_judge(
    verdict: EnumIndependentJudgeVerdict, calls: list[ModelAnswerJudgeCall]
) -> _AnswerJudge:
    def _judge_call(call: ModelAnswerJudgeCall) -> ModelIndependentJudgeVerdict:
        calls.append(call)
        return ModelIndependentJudgeVerdict(
            backend_id=call.backend_id,
            model_id=call.model_id,
            verdict=verdict,
            reason="recorded",
            tokens_in=300,
            tokens_out=30,
        )

    return _judge_call


@pytest.mark.usefixtures("stub_provider_quota_reader")
def test_dispatch_without_a_judge_keeps_the_contract_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _dispatch_summary(tmp_path, monkeypatch, None)
    assert result["quality_gate_passed"] is True
    assert result["status"] == "completed"


@pytest.mark.usefixtures("stub_provider_quota_reader")
def test_dispatch_accepts_a_summary_its_independent_judge_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[ModelAnswerJudgeCall] = []
    judge = _recording_judge(EnumIndependentJudgeVerdict.ACCEPT, calls)
    result = _dispatch_summary(tmp_path, monkeypatch, judge)
    assert result["quality_gate_passed"] is True
    assert result["status"] == "completed"
    assert len(calls) == 1
    call = calls[0]
    assert model_family(call.model_id) not in (None, "qwen")
    assert _SUMMARY in call.user_prompt
    assert "nightly job passed afterwards" in call.user_prompt


@pytest.mark.usefixtures("stub_provider_quota_reader")
def test_dispatch_refuses_a_summary_its_independent_judge_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[ModelAnswerJudgeCall] = []
    judge = _recording_judge(EnumIndependentJudgeVerdict.REJECT, calls)
    result = _dispatch_summary(tmp_path, monkeypatch, judge)
    assert result["quality_gate_passed"] is False
    assert result["status"] == "failed"


@pytest.mark.usefixtures("stub_provider_quota_reader")
def test_dispatch_refuses_when_the_judge_call_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _broken(_call: ModelAnswerJudgeCall) -> ModelIndependentJudgeVerdict:
        raise TimeoutError("judge did not answer")

    result = _dispatch_summary(tmp_path, monkeypatch, _broken)
    assert result["quality_gate_passed"] is False
    assert result["status"] == "failed"


# --- the contract declares the gate's bus surface ----------------------------


def test_contract_declares_the_answer_gate_topics_and_handler() -> None:
    from omnimarket.nodes.node_delegation_answer_gate_compute.handlers.judge_selection import (
        answer_gate_contract,
    )

    contract = answer_gate_contract()
    assert (
        contract["terminal_event"] == "onex.evt.omnimarket.delegation-answer-gated.v1"
    )
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.delegation-answer-gated.v1"
    ]
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.delegation-answer-gate-requested.v1"
    ]
    assert contract["handler"]["class"] == HandlerDelegationAnswerGate.__name__
    assert set(contract["outputs"]) == {
        "accepted",
        "contract_passed",
        "judge",
        "judge_independent",
        "tests_passed",
        "rejection_reasons",
        "fallback_recommended",
    }
