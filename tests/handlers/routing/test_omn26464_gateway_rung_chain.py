# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gateway rung chain: local, then OpenRouter, then the Claude API (OMN-26464).

Each test drives the delegation skill handler through the readiness probe harness with a three rung ladder, makes the
rungs before the answering one unreachable, and reads the terminal's receipt (its attempt lineage) for the rung that
answered. Nothing here touches a real server or a production key: a scripted rung answers a typed reply, and a
``stopped`` rung on the loopback transport is a port nobody listens on.

The three cases:

* the local rung is unreachable, the OpenRouter rung answers and the Claude API rung is never called;
* the local and OpenRouter rungs are unreachable and the Claude API rung answers;
* the customer's own key is rejected (401 or 403) and the run is refused on that rung with no other provider called.
  The control for the last case is the first: a 503 on the same first rung climbs, so a climb was available.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers.delegation_readiness_probe import (
    LegTransportScope,
    LoopbackStubServers,
    ModelLegOutcome,
    ModelLegSpec,
    ModelRungScript,
    ScriptedLegTransport,
    run_leg,
)

pytestmark = pytest.mark.unit

ANSWER_MARKER = "CHAIN-FIXED-ANSWER-26464"
ANSWER = (
    f"### ANSWER\n\nA receipt names the rung that answered. Marker {ANSWER_MARKER}."
)
LOCAL = "local-coder"
OPENROUTER = "openrouter-nemotron-ultra"
CLAUDE_API = "claude-api"


def _local(**kw: object) -> ModelRungScript:
    return ModelRungScript(
        backend_id=LOCAL,
        model_id="Qwen3.8-27B",
        tier="local",
        timeout_ms=5000,
        **kw,  # type: ignore[arg-type]
    )


def _openrouter(**kw: object) -> ModelRungScript:
    return ModelRungScript(
        backend_id=OPENROUTER,
        model_id="nvidia/nemotron-ultra",
        tier="cheap_cloud",
        cost_class="paid",
        scripted_cost_usd="0.0004",
        timeout_ms=5000,
        **kw,  # type: ignore[arg-type]
    )


def _claude(**kw: object) -> ModelRungScript:
    return ModelRungScript(
        backend_id=CLAUDE_API,
        model_id="claude-sonnet-5-5",
        tier="claude",
        cost_class="paid",
        scripted_cost_usd="0.0123",
        timeout_ms=5000,
        **kw,  # type: ignore[arg-type]
    )


def _leg(name: str, *rungs: ModelRungScript) -> ModelLegSpec:
    return ModelLegSpec(
        name=name,
        task_type="summarization",
        prompt="Summarize in one sentence why a receipt names the rung that answered.",
        answer=ANSWER,
        rungs=rungs,
    )


def _run_scripted(
    leg: ModelLegSpec, tmp_path: Path
) -> tuple[ModelLegOutcome, list[str]]:
    transport = ScriptedLegTransport()
    with LegTransportScope(transport) as scope:
        outcome = run_leg(leg, scope, work_dir=tmp_path, tenant_slug="chainproof")
    called = [url.rsplit("/", 1)[-1] for url in transport.calls]
    return outcome, called


def _answering(outcome: ModelLegOutcome) -> str:
    accepted = [a.backend_id for a in outcome.attempts if a.accepted]
    assert len(accepted) == 1, f"{len(accepted)} accepted attempts: {outcome.walked}"
    return accepted[0]


def test_openrouter_answers_when_the_local_rung_is_unreachable(tmp_path: Path) -> None:
    leg = _leg(
        "local_down",
        _local(behaviour="status", status_code=503),
        _openrouter(behaviour="answer"),
        _claude(behaviour="answer"),
    )
    outcome, called = _run_scripted(leg, tmp_path)
    assert outcome.status == "completed"
    assert _answering(outcome) == OPENROUTER
    assert outcome.walked == (LOCAL, OPENROUTER)
    assert called == [LOCAL, OPENROUTER], "the Claude API rung must not be called"
    assert ANSWER_MARKER in outcome.final_answer
    failed = outcome.attempts[0]
    assert failed.failure_class == "model_unavailable"
    assert failed.accepted is False


def test_claude_api_answers_when_local_and_openrouter_are_unreachable(
    tmp_path: Path,
) -> None:
    leg = _leg(
        "local_and_openrouter_down",
        _local(behaviour="status", status_code=503),
        _openrouter(behaviour="status", status_code=503),
        _claude(behaviour="answer"),
    )
    outcome, called = _run_scripted(leg, tmp_path)
    assert outcome.status == "completed"
    assert _answering(outcome) == CLAUDE_API
    assert outcome.walked == (LOCAL, OPENROUTER, CLAUDE_API)
    assert called == [LOCAL, OPENROUTER, CLAUDE_API]
    assert [a.failure_class for a in outcome.attempts[:2]] == ["model_unavailable"] * 2


def test_control_every_rung_unreachable_answers_nothing(tmp_path: Path) -> None:
    leg = _leg(
        "all_down",
        _local(behaviour="status", status_code=503),
        _openrouter(behaviour="status", status_code=503),
        _claude(behaviour="status", status_code=503),
    )
    outcome, called = _run_scripted(leg, tmp_path)
    assert called == [LOCAL, OPENROUTER, CLAUDE_API]
    assert not [a for a in outcome.attempts if a.accepted]
    assert ANSWER_MARKER not in outcome.final_answer


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_own_key_is_refused_and_no_other_provider_is_called(
    tmp_path: Path, status: int
) -> None:
    leg = _leg(
        f"bad_key_{status}",
        _openrouter(behaviour="status", status_code=status),
        _claude(behaviour="answer"),
    )
    # The first rung is the customer's own provider; the Claude API rung is the successor that must stay untouched.
    outcome, called = _run_scripted(leg, tmp_path)
    assert called == [OPENROUTER], f"a rejected key climbed: {called}"
    assert outcome.status == "failed"
    assert not [a for a in outcome.attempts if a.accepted]
    assert CLAUDE_API not in outcome.walked
    assert ANSWER_MARKER not in outcome.final_answer
    assert outcome.walked == (OPENROUTER,)
    assert outcome.attempts[0].failure_class == "provider_auth_failed"


def test_loopback_openrouter_answers_when_the_local_port_is_dead(
    tmp_path: Path,
) -> None:
    """The real delegation effect over real HTTP: the local rung is a port nobody listens on."""
    leg = _leg(
        "loopback_local_dead",
        _local(behaviour="stopped"),
        _openrouter(behaviour="answer"),
        _claude(behaviour="answer"),
    )
    with LegTransportScope(LoopbackStubServers()) as scope:
        outcome = run_leg(leg, scope, work_dir=tmp_path, tenant_slug="chainproof")
    assert outcome.status == "completed"
    assert _answering(outcome) == OPENROUTER
    assert outcome.walked == (LOCAL, OPENROUTER)
    assert ANSWER_MARKER in outcome.final_answer


def test_loopback_claude_api_answers_when_local_and_openrouter_ports_are_dead(
    tmp_path: Path,
) -> None:
    leg = _leg(
        "loopback_two_dead",
        _local(behaviour="stopped"),
        _openrouter(behaviour="stopped"),
        _claude(behaviour="answer"),
    )
    with LegTransportScope(LoopbackStubServers()) as scope:
        outcome = run_leg(leg, scope, work_dir=tmp_path, tenant_slug="chainproof")
    assert outcome.status == "completed"
    assert _answering(outcome) == CLAUDE_API
    assert outcome.walked == (LOCAL, OPENROUTER, CLAUDE_API)


@pytest.mark.parametrize("status", [401, 403])
def test_loopback_a_rejected_own_key_is_refused_with_no_climb(
    tmp_path: Path, status: int
) -> None:
    leg = _leg(
        f"loopback_bad_key_{status}",
        _openrouter(behaviour="status", status_code=status),
        _claude(behaviour="answer"),
    )
    with LegTransportScope(LoopbackStubServers()) as scope:
        outcome = run_leg(leg, scope, work_dir=tmp_path, tenant_slug="chainproof")
    assert outcome.status == "failed"
    assert outcome.walked == (OPENROUTER,)
    assert outcome.attempts[0].failure_class == "provider_auth_failed"
    assert ANSWER_MARKER not in outcome.final_answer
