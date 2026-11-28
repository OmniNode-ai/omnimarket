# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""C13 and C14 show the declared fall-through: local least busy, then OpenRouter, then Claude (OMN-26043).

Before this leg a chain with no usable local responder read NOT_MEASURED, so no check ever showed the chain advance. The leg
sends one task with the local rung made unusable for that run (the declared override of the resolver, never a stopped host)
and reads that OpenRouter answered, recording the attempt list.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.handlers.routing.delegation_check_responder_chain import (
    ModelChainResolution,
    ModelResponderReading,
    resolve_chain,
)
from omnimarket.models.overlay_documents.model_delegation_check_responders_overlay import (
    ModelDelegationCheckResponder,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_delegation_check import (
    AbsentCustomerMachine,
    InMemoryCheckPublisher,
    InMemoryCustomerMachine,
    InMemoryResponderReader,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.grade_delegation_checks import (
    grade_fall_through,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.handler_delegation_check_probe import (
    HandlerDelegationCheckProbe,
    contract_topics,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    CheckId,
    ModelDelegateObservation,
    ModelDelegationCheckRequest,
)

BASE = "http://127.0.0.12:8000"
OTHER = "http://127.0.0.13:8000"
CHAIN = (
    ModelDelegationCheckResponder(tier="local", base_url=BASE),
    ModelDelegationCheckResponder(tier="local", base_url=OTHER),
    ModelDelegationCheckResponder(tier="openrouter"),
    ModelDelegationCheckResponder(tier="claude_api"),
)
READING = ModelResponderReading(
    base_url=BASE, answered=True, served_model="m", n_ctx=4096
)


def _never_read(base_url: str) -> ModelResponderReading:
    raise AssertionError(f"the unusable local rung {base_url} was read")


def test_the_override_makes_every_local_rung_unusable_without_reading_it() -> None:
    resolution = resolve_chain(CHAIN, _never_read, local_unusable=True)
    assert resolution.tier == "openrouter"
    assert not resolution.is_local
    assert [(a.tier, a.outcome) for a in resolution.attempts] == [
        ("local", "unusable-by-override"),
        ("local", "unusable-by-override"),
        ("openrouter", "advanced-to-tier"),
        ("claude_api", "advanced-to-tier"),
    ]


def test_without_the_override_a_usable_local_rung_is_still_chosen() -> None:
    resolution = resolve_chain(CHAIN[:1], lambda _u: READING)
    assert resolution.tier == "local"


def _observation(tiers: tuple[str, ...], receipt_tier: str | None) -> Any:
    return ModelDelegateObservation(
        rc=0,
        receipt_tier=receipt_tier,
        attempt_tiers=tiers,
        accepted_last=True,
    )


def _failed(clauses: Any) -> list[str]:
    return [c.name for c in clauses if not c.ok]


def test_known_good_openrouter_answered_after_the_local_rung_was_tried_and_refused() -> (
    None
):
    resolution = resolve_chain(CHAIN, _never_read, local_unusable=True)
    clauses = grade_fall_through(
        _observation(("local", "openrouter"), "openrouter"), resolution
    )
    assert clauses
    assert _failed(clauses) == []


@pytest.mark.parametrize(
    ("tiers", "receipt_tier", "expected"),
    [
        # Claude answered: OpenRouter was skipped, the declared order is broken.
        (("local", "claude_api"), "claude_api", "fall_through_openrouter_answered"),
        # The local rung answered although it was made unusable for the run.
        (("local",), "local", "fall_through_openrouter_answered"),
        # No attempt list on the receipt: the fall-through is unrecorded.
        ((), "openrouter", "fall_through_attempts_recorded"),
        # The attempts run out of the declared order (Claude before OpenRouter).
        (
            ("local", "claude_api", "openrouter"),
            "openrouter",
            "fall_through_attempts_recorded",
        ),
    ],
)
def test_known_bad_readings_fail_by_name(
    tiers: tuple[str, ...], receipt_tier: str, expected: str
) -> None:
    resolution = resolve_chain(CHAIN, _never_read, local_unusable=True)
    clauses = grade_fall_through(_observation(tiers, receipt_tier), resolution)
    assert expected in _failed(clauses)


def test_a_resolution_whose_local_rung_stayed_usable_fails_the_override_clause() -> (
    None
):
    resolution = resolve_chain(CHAIN[:1], lambda _u: READING)
    clauses = grade_fall_through(
        _observation(("local", "openrouter"), "openrouter"), resolution
    )
    assert "fall_through_local_rung_unusable" in _failed(clauses)


class _FallThroughMachine(InMemoryCustomerMachine):
    def __init__(self, observation: ModelDelegateObservation) -> None:
        super().__init__()
        self._fall = observation
        self.sent: list[ModelChainResolution] = []

    def run_fall_through(
        self, check_id: CheckId, resolution: ModelChainResolution
    ) -> ModelDelegateObservation:
        self.sent.append(resolution)
        return self._fall


def _handler(
    machine: Any, fall_through_checks: Any = ("C13", "C14")
) -> HandlerDelegationCheckProbe:
    return HandlerDelegationCheckProbe(
        fall_through_checks=fall_through_checks,
        responders=CHAIN,
        reader=InMemoryResponderReader({BASE: READING}),
        machine=machine,
        publisher=InMemoryCheckPublisher(),
        topic=contract_topics()[0],
    )


@pytest.mark.parametrize("check_id", ["C13", "C14"])
def test_the_handler_runs_the_leg_and_records_the_attempt_list(check_id: Any) -> None:
    machine = _FallThroughMachine(_observation(("local", "openrouter"), "openrouter"))
    result = _handler(machine).handle(
        ModelDelegationCheckRequest(
            check_id=check_id, trigger="schedule", correlation_id="t"
        )
    )
    names = {c.name: c.ok for c in result.clauses}
    assert names["fall_through_openrouter_answered"] is True
    assert names["fall_through_attempts_recorded"] is True
    assert len(machine.sent) == 1
    assert [a.outcome for a in machine.sent[0].attempts][:2] == [
        "unusable-by-override",
        "unusable-by-override",
    ]
    assert any(
        "openrouter" in c.detail
        for c in result.clauses
        if c.name.startswith("fall_through")
    )


@pytest.mark.parametrize("check_id", ["C13", "C14"])
def test_the_handler_fails_the_leg_when_claude_answered(check_id: Any) -> None:
    machine = _FallThroughMachine(_observation(("local", "claude_api"), "claude_api"))
    result = _handler(machine).handle(
        ModelDelegationCheckRequest(
            check_id=check_id, trigger="schedule", correlation_id="t"
        )
    )
    assert "fall_through_openrouter_answered" in [
        c.name for c in result.clauses if not c.ok
    ]


def test_a_machine_with_no_fall_through_collector_claims_no_clause_of_the_leg() -> None:
    result = _handler(AbsentCustomerMachine("none")).handle(
        ModelDelegationCheckRequest(
            check_id="C13", trigger="schedule", correlation_id="t"
        )
    )
    assert not [c for c in result.clauses if c.name.startswith("fall_through")]
    assert result.fall_through_attempts == ()


@pytest.mark.parametrize("check_id", ["C13", "C14"])
def test_with_no_declared_setting_the_leg_is_off_and_no_task_is_sent(
    check_id: Any,
) -> None:
    machine = _FallThroughMachine(_observation(("local", "openrouter"), "openrouter"))
    result = _handler(machine, fall_through_checks=()).handle(
        ModelDelegationCheckRequest(
            check_id=check_id, trigger="schedule", correlation_id="t"
        )
    )
    assert machine.sent == []
    assert not [c for c in result.clauses if c.name.startswith("fall_through")]


def test_a_check_the_setting_does_not_name_gets_no_leg() -> None:
    machine = _FallThroughMachine(_observation(("local", "openrouter"), "openrouter"))
    _handler(machine, fall_through_checks=("C13",)).handle(
        ModelDelegationCheckRequest(
            check_id="C14", trigger="schedule", correlation_id="t"
        )
    )
    assert machine.sent == []


def test_the_nodes_contract_declares_the_setting_that_turns_the_leg_on() -> None:
    from omnimarket.nodes.node_delegation_check_probe_effect.handlers.handler_delegation_check_probe import (
        contract_fall_through,
    )

    declared = contract_fall_through()
    assert declared is not None
    assert declared.checks == ("C13", "C14")
    assert declared.secret_ref == "llm.openrouter.api_key"


def test_an_overlay_block_answers_before_the_contract_declaration() -> None:
    from omnimarket.models.overlay_documents.model_delegation_check_probe_overlay import (
        ModelDelegationCheckProbeOverlay,
        ModelFallThroughSettings,
    )
    from omnimarket.nodes.node_delegation_check_probe_effect import (
        runtime_delegation_check_probe_tick as tick,
    )

    base = {
        "checks": ["C13"],
        "history_file": "h.jsonl",
        "history_keep": 1,
        "result_topic": "t",
        "responder_timeout_s": 1,
    }
    own = ModelFallThroughSettings(
        checks=("C14",), secret_ref="llm.x.api_key", key_env="K_ENV"
    )
    with_block = ModelDelegationCheckProbeOverlay.model_validate(
        {**base, "fall_through": own.model_dump()}
    )
    assert tick._fall_through_setting(with_block) == own
    declared = tick._fall_through_setting(
        ModelDelegationCheckProbeOverlay.model_validate(base)
    )
    assert declared is not None
    assert declared.checks == ("C13", "C14")
    key = tick._fall_through_key(with_block)
    assert key is not None
    assert key.secret_ref == "llm.x.api_key"
