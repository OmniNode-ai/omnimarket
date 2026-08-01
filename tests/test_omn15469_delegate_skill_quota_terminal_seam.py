# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Producer-side seam test for OMN-15469 — truthful quota terminal emission.

Drives the REAL producer entrypoint (``HandlerDelegateSkill.handle``, the
definition-B handler node_delegate_skill_orchestrator actually runs) over a
frozen forced-429 dispatch-port capture of ONE accepted delegate-skill command.
No provider is contacted and no runtime lane is touched.

This is the PRODUCER half of the pair whose CONSUMER half is
``tests/test_omn15503_quota_terminal_seam.py`` (OMN-15503, omnimarket#1995).
Both replay the same frozen forced-429 command (same correlation id, same
attempt ladder, same 429 strings); this file asserts what goes ON the wire,
that file asserts what the projection makes of it.

Seam under test — dispatch-port result -> definition-B return value -> the
contract's terminal topic, field by field:

* ``result["status"]`` (port-reported) -> ``ModelDelegateSkillResponse.status``
  CORRECTED by the composite verdict, never forwarded verbatim
* ``result["attempts"][].failure_class`` (str) ->
  ``terminal_failure_cause`` (typed ``EnumDelegationTerminalFailureCause``)
* returned model CLASS NAME (``ModelDelegateSkillFailed``) ->
  ``published_events[].event_type`` (``DelegateSkillFailed``) -> failure topic.
  The runtime resolves this with ``class_name.removeprefix("Model")``
  (``DispatchResultApplier._resolve_mapped_output_topic``), so the class name
  and the contract entry must agree character for character; this test pins
  that agreement against the contract file rather than a hand-written constant.

RED (pre-fix, the 2026-07-29 matrix row ``refactor | FAIL | Google Gemini HTTP
429 after two escalation attempts``): the handler forwarded the port's
``status="completed"`` / ``quality_gate_passed=true`` verbatim, returned a bare
``ModelDelegateSkillResponse`` whose class name misses the published-events map,
and the wiring fell back to the contract's SUCCESS terminal — so an
all-attempts-429 command published ``delegate-skill-completed`` and the outer
``/skill`` reported ``ok=true``. No typed cause existed anywhere on the wire.

GREEN: exactly one terminal, of the failure class, on the failure topic, with
``status="failed"`` and the typed
``EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED``.
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
    ModelDelegateSkillAttemptRecord,
    ModelDelegateSkillCompleted,
    ModelDelegateSkillFailed,
    ModelDelegateSkillResponse,
    delegate_skill_terminal_from_response,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "seams"
    / "quota_terminal"
    / "forced_429_dispatch_result.json"
)

# The consumer half's fixture (OMN-15503 / omnimarket#1995). Present only once
# that PR has landed; the cross-check below is skipped with a named reason
# rather than asserted vacuously when it is absent.
CONSUMER_FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "seams"
    / "quota_terminal"
    / "forced_429_terminal.json"
)

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_delegate_skill_orchestrator"
    / "contract.yaml"
)


def _load_fixture() -> dict[str, Any]:
    """Load the frozen forced-429 dispatch-port capture.

    Fails loudly (never skips) when the fixture is absent — a missing fixture
    must not be reported as a passing seam.
    """
    if not FIXTURE_PATH.exists():  # pragma: no cover - guard
        raise AssertionError(
            f"forced-429 producer seam fixture missing: {FIXTURE_PATH}"
        )
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class _FrozenQuotaDispatchPort:
    """Dispatch port that replays the frozen forced-429 result verbatim.

    Also counts calls, so "exactly one accepted command produced exactly one
    terminal" is measured rather than assumed.
    """

    def __init__(self, result: dict[str, object]) -> None:
        self._result = result
        self.calls = 0

    async def dispatch(self, **_kwargs: object) -> dict[str, object]:
        self.calls += 1
        return dict(self._result)


def _run_producer() -> tuple[dict[str, Any], ModelDelegateSkillResponse, int]:
    fixture = _load_fixture()
    port = _FrozenQuotaDispatchPort(fixture["dispatch_result"])
    handler = HandlerDelegateSkill(dispatch_port=port)  # type: ignore[arg-type]
    request = ModelDelegateSkillRequest(
        prompt=fixture["prompt_text"],
        task_type=fixture["task_type"],
        source="external-client",
        correlation_id=UUID(fixture["correlation_id"]),
        session_id=fixture["session_id"],
        tenant_id=fixture["tenant_id"],
    )
    terminal = asyncio.run(handler.handle(request))
    return fixture, terminal, port.calls


def _published_events_map() -> dict[str, str]:
    """event_type short-name -> topic, read from the contract the runtime reads."""
    raw = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    return {
        str(entry["event_type"]): str(entry["topic"])
        for entry in raw.get("published_events", [])
    }


@pytest.mark.unit
def test_seam_quota_exhausted_command_emits_one_typed_failure_terminal() -> None:
    """A forced-429 command emits exactly one typed failure terminal.

    Four assertions, matching the OMN-15469 acceptance contract:

    1. exactly ONE terminal for the accepted command — the definition-B return
       value is the sole producer (the handler self-publishes nothing);
    2. that terminal's class routes to the contract's FAILURE topic, resolved
       through the same ``removeprefix("Model")`` rule the runtime applies;
    3. ``status`` is ``failed`` despite the dispatch port reporting
       ``completed`` — the composite verdict corrects the lie at the boundary;
    4. ``terminal_failure_cause`` is the typed
       ``PROVIDER_QUOTA_EXHAUSTED`` enum value, not a generic string.
    """
    fixture, terminal, dispatch_calls = _run_producer()
    expected = fixture["expected_terminal"]

    # 1. One accepted command, one dispatch, one returned terminal.
    assert dispatch_calls == expected["durable_terminal_events"], (
        f"expected exactly {expected['durable_terminal_events']} dispatch/terminal "
        f"per accepted command, got {dispatch_calls}"
    )

    # 2. Failure class identity -> contract-declared failure topic.
    assert type(terminal).__name__ == expected["terminal_model_class"], (
        "the definition-B return value must be the typed FAILURE terminal "
        f"variant; got {type(terminal).__name__} "
        f"(status={terminal.status!r}, "
        f"quality_gate_passed={terminal.quality_gate_passed!r})"
    )
    assert not isinstance(terminal, ModelDelegateSkillCompleted)
    published = _published_events_map()
    routed_topic = published.get(type(terminal).__name__.removeprefix("Model"))
    assert routed_topic == expected["terminal_topic"], (
        "the returned class name must resolve to the contract's failure topic "
        "through the runtime's published_events map "
        f"(class={type(terminal).__name__}, resolved={routed_topic!r}, "
        f"map={published!r})"
    )

    # 3. Truthful status, so the outer /skill reports ok=false.
    assert terminal.status == expected["status"], (
        "an all-attempts-429 command must not terminalize as completed; the "
        f"dispatch port reported {fixture['dispatch_result']['status']!r} and "
        f"the terminal carries {terminal.status!r}"
    )
    assert terminal.quality_gate_passed is expected["quality_gate_passed"], (
        "a quota-exhausted terminal must not claim the quality gate passed"
    )

    # 4. Typed cause, not a generic string.
    assert terminal.terminal_failure_cause is (
        EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED
    ), (
        "terminal must carry the typed "
        "EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED; got "
        f"{terminal.terminal_failure_cause!r} "
        f"(error_message={terminal.error_message!r})"
    )
    assert len(terminal.attempts) >= expected["attempt_history_min_length"], (
        "the typed attempt ladder must survive onto the terminal so "
        "'429'd after two escalations' is provable from the wire record"
    )


@pytest.mark.unit
def test_successful_delegation_still_terminalizes_as_completed() -> None:
    """The composite verdict does not turn honest successes into failures.

    Guards the fail-closed rule from over-firing: a passing attempt ladder with
    ``status="completed"`` must still produce the COMPLETED variant on the
    success topic, otherwise the fix trades one lie for its mirror image.
    """
    fixture = _load_fixture()
    result = dict(fixture["dispatch_result"])
    result["attempts"] = [
        {
            "tier": "local",
            "backend_id": "mlx-local",
            "model_id": "qwen3-35b",
            "quality_gate_passed": True,
            "cost_usd": 0.0,
            "error_message": "",
        }
    ]
    result["content"] = "refactored"
    port = _FrozenQuotaDispatchPort(result)
    handler = HandlerDelegateSkill(dispatch_port=port)  # type: ignore[arg-type]
    terminal = asyncio.run(
        handler.handle(
            ModelDelegateSkillRequest(
                prompt=fixture["prompt_text"],
                task_type=fixture["task_type"],
                source="external-client",
                correlation_id=UUID(fixture["correlation_id"]),
            )
        )
    )

    assert isinstance(terminal, ModelDelegateSkillCompleted)
    assert terminal.status == "completed"
    assert terminal.terminal_failure_cause is None
    published = _published_events_map()
    assert (
        published[type(terminal).__name__.removeprefix("Model")]
        == "onex.evt.omnimarket.delegate-skill-completed.v1"
    )


@pytest.mark.unit
def test_escalation_history_fallback_does_not_hide_completed_terminal() -> None:
    """Incomplete escalation-history fallback is not proof of terminal failure."""
    terminal = delegate_skill_terminal_from_response(
        ModelDelegateSkillResponse(
            status="completed",
            correlation_id=UUID("3f9d2c17-8b4a-4e51-9a26-7c0d5e1b8f34"),
            task_type="refactor",
            quality_gate_passed=True,
            quality_score=0.91,
            required_quality_bar=0.8,
            score_vs_required_bar="at_or_above_bar",
            attempts_count=2,
            attempts=[
                ModelDelegateSkillAttemptRecord(
                    tier="local",
                    backend_id="mlx-local",
                    model_id="qwen3-35b",
                    quality_gate_passed=False,
                    error_message="rejected before terminal attempt",
                )
            ],
        )
    )

    assert isinstance(terminal, ModelDelegateSkillCompleted)
    assert terminal.status == "completed"


@pytest.mark.unit
def test_failed_variant_rejects_successful_evidence() -> None:
    """A failed-class terminal cannot carry success evidence."""
    with pytest.raises(ValueError, match="failed delegation requires"):
        ModelDelegateSkillFailed(
            correlation_id=UUID("3f9d2c17-8b4a-4e51-9a26-7c0d5e1b8f34"),
            task_type="refactor",
            quality_gate_passed=True,
            attempts=[
                ModelDelegateSkillAttemptRecord(
                    tier="local",
                    backend_id="mlx-local",
                    model_id="qwen3-35b",
                    quality_gate_passed=True,
                )
            ],
        )


@pytest.mark.unit
def test_completed_negative_verdict_builds_consistent_failed_payload() -> None:
    """A corrected failed payload remains internally valid."""
    terminal = delegate_skill_terminal_from_response(
        ModelDelegateSkillResponse(
            status="completed",
            correlation_id=UUID("3f9d2c17-8b4a-4e51-9a26-7c0d5e1b8f34"),
            task_type="refactor",
            quality_gate_passed=True,
            quality_score=0.91,
            required_quality_bar=0.8,
            score_vs_required_bar="at_or_above_bar",
            attempts_count=1,
            attempts=[
                ModelDelegateSkillAttemptRecord(
                    tier="local",
                    backend_id="mlx-local",
                    model_id="qwen3-35b",
                    quality_gate_passed=False,
                    failure_class="provider_quota_exhausted",
                    error_message="429 quota exceeded",
                )
            ],
        )
    )

    assert isinstance(terminal, ModelDelegateSkillFailed)
    assert terminal.status == "failed"
    assert terminal.failed_acceptance_criteria


@pytest.mark.unit
def test_dispatch_exception_terminalizes_as_failed_variant() -> None:
    """A dispatch exception still produces the typed FAILURE variant.

    Before this change the exception path returned a bare
    ``ModelDelegateSkillResponse``, whose class name misses the published-events
    map — so the wiring published a hard dispatch failure onto the contract's
    SUCCESS terminal.
    """

    class _ExplodingPort:
        async def dispatch(self, **_kwargs: object) -> dict[str, object]:
            raise RuntimeError("runtime dispatch refused")

    handler = HandlerDelegateSkill(dispatch_port=_ExplodingPort())  # type: ignore[arg-type]
    terminal = asyncio.run(
        handler.handle(
            ModelDelegateSkillRequest(
                prompt="p", task_type="refactor", source="external-client"
            )
        )
    )

    assert isinstance(terminal, ModelDelegateSkillFailed)
    assert terminal.status == "failed"
    assert "runtime dispatch refused" in terminal.error_message


@pytest.mark.unit
@pytest.mark.skipif(
    not CONSUMER_FIXTURE_PATH.exists(),
    reason=(
        "consumer-half fixture ships in omnimarket#1995 (OMN-15503); the "
        "producer assertions above run unconditionally, this cross-check only "
        "pins the two halves against each other once both are on dev"
    ),
)
def test_producer_terminal_matches_consumer_fixture_expectations() -> None:
    """The emitted terminal satisfies what the consumer fixture expects to see.

    The two halves are frozen captures of the SAME command, so the producer's
    output must satisfy the consumer fixture's ``expected_projection`` contract
    field-for-field. This is the check that catches the halves drifting apart.
    """
    consumer = json.loads(CONSUMER_FIXTURE_PATH.read_text(encoding="utf-8"))
    _fixture, terminal, _calls = _run_producer()

    assert str(terminal.correlation_id) == consumer["correlation_id"]
    projection = consumer["expected_projection"]
    assert projection["terminal_ok"] is False
    assert terminal.status != "completed"
    assert terminal.terminal_failure_cause is not None
    assert (
        terminal.terminal_failure_cause.value == projection["terminal_failure_cause"]
    ), (
        "producer's typed cause and consumer's expected projected cause must be "
        "the same enum value"
    )
    assert len(terminal.attempts) >= projection["attempt_history_min_length"]
    assert all(
        attempt.failure_class == projection["terminal_failure_cause"]
        for attempt in terminal.attempts
    ), "every 429'd attempt must carry the typed failure_class the consumer reduces"
