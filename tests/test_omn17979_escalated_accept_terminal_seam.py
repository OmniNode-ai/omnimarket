# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Producer-side seam test for OMN-17979 — an escalated success is constructible.

Drives the REAL producer entrypoint (``HandlerDelegateSkill.handle``, the
definition-B handler ``node_delegate_skill_orchestrator`` actually runs) over a
frozen capture of ONE live delegation terminal in which the ladder ABANDONED its
local rung and the NEXT rung answered. No provider is contacted and no runtime
lane is touched.

The fixture is a verbatim readback of
``onex.evt.omnibase-infra.delegation-completed.v1`` offset 64, correlation
``23c2721b-84d3-498b-803d-1855f4bb5df6``, taken read-only off the ``.201`` dev
lane broker on 2026-09-06. Its two rungs are the whole point:

* rung[0] ``tier_name=local`` ``model_used=qwen3.8``
  ``acceptance_decision=climb``, ``failure_reasons=["provider HTTP 404 Not Found
  ... model `qwen3.8` does not exist"]`` — the rung the ladder abandoned;
* rung[1] ``tier_name=cheap_cloud`` ``model_used=gemini-2.5-flash-lite``
  ``acceptance_decision=accept``, ``quality_score=1.0`` at ``required_bar=0.8``
  — the rung that answered, and the terminal.

RED (pre-fix): ``_response_from_result`` fed that WHOLE ladder to
``resolve_terminal_failure_cause``. The abandoned rung's 404 text is observed
evidence, so the classifier returned ``PROVIDER_ERROR`` for a run that terminally
succeeded, and the handler then forced ``quality_gate_passed=False`` while
``quality_score=1.0`` / ``required_quality_bar=0.8`` /
``score_vs_required_bar=at_or_above_bar`` stayed as the ACCEPTED rung had
reported them and ``failed_acceptance_criteria`` stayed empty. That triple is
exactly what ``ModelDelegateSkillResponse``'s validator refuses, so
``handle()`` raised ``ValidationError`` and the command terminalized as an
auto-wiring boundary failure. ``delegate-skill-completed.v1`` took zero records.

GREEN: evidence from a rung the ladder abandoned is not the terminal's failure
cause. A ladder that records an acceptance has no derived terminal failure, the
response constructs with ``quality_gate_passed=True``, and it is the COMPLETED
class that routes to the success topic.

The validator is NOT relaxed here — the producer is made to satisfy it. The
counter-tests below pin that: an all-rungs-refused ladder still classifies a
typed cause and still terminalizes as FAILED, and a terminal that genuinely
fails at or above the bar names the criterion it failed rather than being
unconstructible.

Hermetic — no provider, no lane, no bus.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from omnibase_core.enums.enum_delegation_terminal_failure_cause import (
    EnumDelegationTerminalFailureCause,
)

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillCompleted,
    ModelDelegateSkillFailed,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "seams"
    / "escalated_success"
    / "omn17979_escalated_accept_dispatch_result.json"
)

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_delegate_skill_orchestrator"
    / "contract.yaml"
)

SUCCESS_TOPIC = "onex.evt.omnimarket.delegate-skill-completed.v1"
FAILURE_TOPIC = "onex.evt.omnimarket.delegate-skill-failed.v1"


def _load_fixture() -> dict[str, Any]:
    """Load the frozen escalated-accept capture.

    Fails loudly (never skips) when the fixture is absent — a missing fixture
    must not be reported as a passing seam.
    """
    if not FIXTURE_PATH.exists():  # pragma: no cover - guard
        raise AssertionError(f"escalated-accept seam fixture missing: {FIXTURE_PATH}")
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _published_events_map() -> dict[str, str]:
    """event_type short-name -> topic, read from the contract the runtime reads."""
    raw = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    return {
        str(entry["event_type"]): str(entry["topic"])
        for entry in raw.get("published_events", [])
    }


class _FrozenDispatchPort:
    """Dispatch port replaying one frozen delegation terminal verbatim."""

    def __init__(self, result: dict[str, object]) -> None:
        self._result = result
        self.calls = 0

    async def dispatch(self, **_kwargs: object) -> dict[str, object]:
        self.calls += 1
        return dict(self._result)


def _run_producer(
    result: dict[str, Any],
    *,
    correlation_id: str,
    prompt: str,
    task_type: str,
) -> ModelDelegateSkillCompleted | ModelDelegateSkillFailed:
    """Run the real definition-B handler over a frozen dispatch-port result."""
    port = _FrozenDispatchPort(result)
    handler = HandlerDelegateSkill(dispatch_port=port)  # type: ignore[arg-type]
    terminal = asyncio.run(
        handler.handle(
            ModelDelegateSkillRequest(
                prompt=prompt,
                task_type=task_type,
                source="external-client",
                correlation_id=UUID(correlation_id),
            )
        )
    )
    assert port.calls == 1, "one accepted command must produce exactly one dispatch"
    return terminal


@pytest.mark.unit
def test_escalated_accept_terminalizes_as_completed_with_quality_gate_passed() -> None:
    """OMN-17979 AC1/AC3: the escalation path produces a constructible success.

    This is the live defect verbatim. The assertion that matters most is that
    ``handle()`` returns at all: pre-fix it raised ``ValidationError`` inside
    ``_response_from_result``, which is why the command never reached a
    delegate-skill terminal of either kind.
    """
    fixture = _load_fixture()
    terminal = _run_producer(
        fixture["dispatch_result"],
        correlation_id=fixture["correlation_id"],
        prompt=fixture["prompt_text"],
        task_type=fixture["task_type"],
    )

    assert isinstance(terminal, ModelDelegateSkillCompleted), (
        "a run whose escalated rung was ACCEPTED at 1.0 against a 0.8 bar must "
        f"terminalize as the COMPLETED variant; got {type(terminal).__name__} "
        f"(status={terminal.status!r}, "
        f"terminal_failure_cause={terminal.terminal_failure_cause!r}, "
        f"error_message={terminal.error_message!r})"
    )
    assert terminal.status == "completed"
    assert terminal.quality_gate_passed is True, (
        "quality_gate_passed must follow the rung that ANSWERED, not the rung "
        "the ladder abandoned"
    )
    assert terminal.terminal_failure_cause is None, (
        "the abandoned local rung's HTTP 404 is escalation history, not the "
        "terminal's failure cause"
    )
    assert terminal.quality_score == 1.0
    assert terminal.required_quality_bar == 0.8
    assert terminal.failed_acceptance_criteria == ()

    published = _published_events_map()
    assert published[type(terminal).__name__.removeprefix("Model")] == SUCCESS_TOPIC, (
        "the COMPLETED class must route to the contract's success terminal topic"
    )


@pytest.mark.unit
def test_abandoned_rung_is_still_legible_on_the_terminal() -> None:
    """The fix drops the false verdict, not the evidence.

    The abandoned rung must survive onto the attempt ladder — "the local rung
    404'd and the ladder climbed" stays provable from the wire record.
    """
    fixture = _load_fixture()
    terminal = _run_producer(
        fixture["dispatch_result"],
        correlation_id=fixture["correlation_id"],
        prompt=fixture["prompt_text"],
        task_type=fixture["task_type"],
    )

    assert terminal.attempts_count == 2
    assert len(terminal.attempts) == 2
    abandoned, accepted = terminal.attempts
    assert abandoned.tier == "local"
    assert abandoned.quality_gate_passed is False
    assert "404" in abandoned.error_message, (
        "the abandoned rung's observed refusal must still be readable on the "
        f"ladder; got {abandoned.error_message!r}"
    )
    assert accepted.tier == "cheap_cloud"
    assert accepted.quality_gate_passed is True


@pytest.mark.unit
def test_all_rungs_refused_still_terminalizes_as_failed() -> None:
    """Positive control: a genuinely failed run still fails.

    Same fixture, same shape, with the ACCEPT rung replaced by a second refusal
    and the terminal's own quality verdict set to the failure it really was. No
    rung was accepted, so the ladder's observed evidence is still the terminal's
    cause and the run must still land on the failure topic.
    """
    fixture = _load_fixture()
    result = dict(fixture["dispatch_result"])
    history = [dict(row) for row in result["escalation_history"]]
    history[1] = {
        **history[1],
        "quality_score": 0.0,
        "acceptance_decision": "climb",
        "acceptance_reason": "provider_call_failed",
        "failure_reasons": [
            "provider HTTP 429 Too Many Requests; "
            'response_body={"error":{"status":"RESOURCE_EXHAUSTED"}}'
        ],
    }
    result["escalation_history"] = history
    result["quality_passed"] = False
    result["quality_score"] = 0.0
    result["score_vs_required_bar"] = "below_bar"
    result["failure_reason"] = "every rung refused"

    terminal = _run_producer(
        result,
        correlation_id=fixture["correlation_id"],
        prompt=fixture["prompt_text"],
        task_type=fixture["task_type"],
    )

    assert isinstance(terminal, ModelDelegateSkillFailed), (
        "a ladder with no accepted rung must still terminalize as FAILED; got "
        f"{type(terminal).__name__}"
    )
    assert terminal.status == "failed"
    assert terminal.quality_gate_passed is False
    assert terminal.terminal_failure_cause is (
        EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED
    ), (
        "a corroborated 429 must still classify as the quota cause; got "
        f"{terminal.terminal_failure_cause!r}"
    )
    published = _published_events_map()
    assert published[type(terminal).__name__.removeprefix("Model")] == FAILURE_TOPIC


@pytest.mark.unit
def test_terminal_failure_at_or_above_bar_names_the_failed_criterion() -> None:
    """OMN-17979 AC2: the validator's premise is satisfied, never weakened.

    When the producer downgrades ``quality_gate_passed`` because a typed
    terminal failure cause was classified, and the score comparison is
    ``at_or_above_bar``, the response must SAY which criterion failed. Pre-fix
    it said nothing and was therefore unconstructible; the fix supplies the real
    reason rather than relaxing the rule that demands one.

    The shape here is the one the wire model refuses on an empty criteria
    tuple: a ladder with no accepted rung, an observed provider refusal, and a
    terminal still reporting a score at or above its bar.
    """
    fixture = _load_fixture()
    result = dict(fixture["dispatch_result"])
    history = [dict(row) for row in result["escalation_history"]]
    history[1] = {
        **history[1],
        "acceptance_decision": "climb",
        "acceptance_reason": "provider_call_failed",
        "failure_reasons": ["provider HTTP 500 Internal Server Error"],
    }
    result["escalation_history"] = history

    terminal = _run_producer(
        result,
        correlation_id=fixture["correlation_id"],
        prompt=fixture["prompt_text"],
        task_type=fixture["task_type"],
    )

    assert terminal.quality_gate_passed is False
    assert terminal.score_vs_required_bar is not None
    assert terminal.score_vs_required_bar.value == "at_or_above_bar"
    assert terminal.terminal_failure_cause is (
        EnumDelegationTerminalFailureCause.PROVIDER_ERROR
    )
    assert terminal.failed_acceptance_criteria, (
        "a quality-failed terminal at or above the bar must name the criterion "
        "it failed — that is the validator's premise, and the producer is what "
        "has to satisfy it"
    )
    assert any(
        EnumDelegationTerminalFailureCause.PROVIDER_ERROR.value in criterion
        for criterion in terminal.failed_acceptance_criteria
    ), (
        "the named criterion must carry the typed cause, not a placeholder; got "
        f"{terminal.failed_acceptance_criteria!r}"
    )
