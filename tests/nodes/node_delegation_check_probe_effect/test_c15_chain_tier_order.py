# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""C15 asserts the tier order of the responder chain: local, then OpenRouter, then Claude (OMN-26043).

The chain audit found C15 graded the run through the gateway and never the order of the chain it tries: a row that named the
Claude API before OpenRouter, or a local responder after a cloud tier, read green. The settings store validates the order when
a row is written; the check reads the row it was handed, so the check asserts it too.
"""

from __future__ import annotations

from typing import Any

import pytest
from _gateway_fake import FakeClock, FakeGateway

from omnimarket.handlers.routing.delegation_check_responder_chain import (
    ModelResponderReading,
)
from omnimarket.models.overlay_documents.model_delegation_check_responders_overlay import (
    ModelDelegationCheckResponder,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_delegation_check import (
    AbsentCustomerMachine,
    InMemoryCheckPublisher,
    InMemoryResponderReader,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.gateway_checks import (
    GatewayRunSettings,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.grade_delegation_checks import (
    grade_chain_tier_order,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.handler_delegation_check_probe import (
    HandlerDelegationCheckProbe,
    contract_topics,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    EnumCheckVerdict,
    ModelDelegationCheckRequest,
)

BASE = "http://127.0.0.12:8000"
LOCAL = ModelDelegationCheckResponder(tier="local", base_url=BASE)
OPENROUTER = ModelDelegationCheckResponder(tier="openrouter")
CLAUDE = ModelDelegationCheckResponder(tier="claude")
_CLOCK = FakeClock()


def _c15(responders: tuple[ModelDelegationCheckResponder, ...]) -> Any:
    handler = HandlerDelegationCheckProbe(
        responders=responders,
        reader=InMemoryResponderReader(
            {
                BASE: ModelResponderReading(
                    base_url=BASE, answered=True, served_model="m", n_ctx=4096
                )
            }
        ),
        machine=AbsentCustomerMachine("none"),
        publisher=InMemoryCheckPublisher(),
        topic=contract_topics()[0],
        gateway=FakeGateway(),
        gateway_settings=GatewayRunSettings(run_budget_s=5.0, poll_interval_s=0.0),
        sleep=_CLOCK.sleep,
        monotonic=_CLOCK.monotonic,
    )
    return handler.handle(
        ModelDelegationCheckRequest(correlation_id="t", check_id="C15")
    )


def test_the_declared_order_local_openrouter_claude_grades_ok() -> None:
    assert grade_chain_tier_order((LOCAL, OPENROUTER, CLAUDE)).ok


@pytest.mark.parametrize(
    "responders",
    [
        (LOCAL, CLAUDE, OPENROUTER),
        (OPENROUTER, LOCAL, CLAUDE),
        (CLAUDE, OPENROUTER, LOCAL),
    ],
)
def test_any_other_order_grades_red_and_names_it(
    responders: tuple[ModelDelegationCheckResponder, ...],
) -> None:
    clause = grade_chain_tier_order(responders)
    assert not clause.ok
    assert "local" in clause.detail
    assert "claude" in clause.detail


def test_c15_carries_the_tier_order_clause_beside_its_gateway_clauses() -> None:
    result = _c15((LOCAL, OPENROUTER, CLAUDE))
    named = {c.name: c.ok for c in result.clauses}
    assert named["responder_chain_tier_order"] is True
    assert result.verdict is EnumCheckVerdict.PASS


def test_known_bad_c15_reads_fail_on_a_chain_naming_the_claude_before_openrouter() -> (
    None
):
    result = _c15((LOCAL, CLAUDE, OPENROUTER))
    assert result.verdict is EnumCheckVerdict.FAIL
    assert {c.name for c in result.clauses if not c.ok} == {
        "responder_chain_tier_order"
    }
