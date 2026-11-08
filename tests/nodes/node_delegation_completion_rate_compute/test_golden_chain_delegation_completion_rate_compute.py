# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain (OMN-23108): the packaged contract resolves to the handler, the above-bound hour reads WORKING with its numbers and
the topics the contract declares, and each named mutation turns the chain red.

Every mutation in NAMED_MUTATIONS replaces one rule in the handler module and runs the same golden assertions; the mutated chain
must raise AssertionError, which shows the assertion can fail.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from omnimarket.models.model_delegation_completion_rate import (
    ModelDelegationCompletionRate,
    ModelDelegationCompletionRateRequest,
)
from omnimarket.models.model_reading import EnumReadingVerdict as V
from omnimarket.nodes.node_delegation_completion_rate_compute.handlers import (
    handler_delegation_completion_rate as module,
)
from tests.nodes.node_delegation_completion_rate_compute.completion_rate_support import (
    above_bound_hour,
    contract,
    event,
    request,
    resolve_handler,
)

pytestmark = pytest.mark.unit

#: mutation name -> (the chain it turns red, the test that shows it).
NAMED_MUTATIONS: dict[str, tuple[str, str]] = {
    "harness-counted-as-deployed": (
        "golden",
        "test_mutation_harness_counted_as_deployed_turns_the_golden_chain_red",
    ),
    "window-ignored": (
        "golden",
        "test_mutation_window_ignored_turns_the_golden_chain_red",
    ),
    "always-failing": (
        "golden",
        "test_mutation_always_failing_turns_the_golden_chain_red",
    ),
}


def run_golden_chain() -> None:
    """The golden assertions: contract routing, the above-bound hour, a row older than the window."""
    bus = contract()["event_bus"]
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.delegation-completion-rate-requested.v1"
    ]
    assert bus["publish_topics"] == [
        "onex.evt.omnimarket.delegation-completion-rate-read.v1"
    ]
    assert contract()["terminal_event"] in bus["publish_topics"]
    handler = resolve_handler()()
    reading = handler.handle(request(events=(*above_bound_hour(), event(90))))
    assert isinstance(reading, ModelDelegationCompletionRate)
    assert reading.verdict is V.WORKING
    assert reading.reasons == ()
    assert (reading.r_all, reading.r_dep, reading.failed_rate) == (136, 100, 0.0)
    assert reading.rows_in_window == 136
    assert (
        " | STATUS | lane=delegation-rate | src=delegation-rate | "
        in reading.ledger_row
    )
    assert "verdict=WORKING" in reading.ledger_row


def test_contract_declares_a_pure_compute_node_with_one_operation() -> None:
    declared = contract()
    assert declared["name"] == "node_delegation_completion_rate_compute"
    assert declared["node_type"] == "compute"
    assert declared["descriptor"]["side_effects"] == []
    [entry] = declared["handler_routing"]["handlers"]
    assert declared["handler_routing"]["routing_strategy"] == "operation_match"
    assert entry["operation"] == "delegation_completion_rate"
    assert resolve_handler() is module.HandlerDelegationCompletionRate
    assert (
        declared["handler"]["input_model"].rpartition(".")[2]
        == ModelDelegationCompletionRateRequest.__name__
    )


def test_contract_holds_no_bound() -> None:
    text = str(contract())
    for word in ("min_r_all", "min_r_dep", "max_failed_rate", "window_seconds"):
        assert word not in text.replace("travel on the request", "")


def test_golden_chain_above_bound_hour_reads_working_through_the_contract_handler() -> (
    None
):
    run_golden_chain()


def _harness_as_deployed(real: Callable[..., Any]) -> Callable[..., Any]:
    def mutated(overlay: Any, rows: Any) -> Any:
        r_all, _r_dep, rate = real(overlay, rows)
        return r_all, r_all, rate

    return mutated


def test_mutation_harness_counted_as_deployed_turns_the_golden_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "count_rates", _harness_as_deployed(module.count_rates))
    with pytest.raises(AssertionError):
        run_golden_chain()


def test_mutation_window_ignored_turns_the_golden_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "in_window", lambda req: (list(req.events), 0))
    with pytest.raises(AssertionError):
        run_golden_chain()


def test_mutation_always_failing_turns_the_golden_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "judge", lambda *_a: ["r_all-below-bound:0<1"])
    with pytest.raises(AssertionError):
        run_golden_chain()


def test_the_golden_chain_is_green_again_after_each_mutation_is_undone() -> None:
    """Positive control: the chain the mutations break is the one that passes unmutated."""
    run_golden_chain()


def test_every_named_golden_mutation_has_its_test() -> None:
    names = {n for n, v in globals().items() if n.startswith("test_") and callable(v)}
    assert {test for _, test in NAMED_MUTATIONS.values()} <= names
    assert {kind for kind, _ in NAMED_MUTATIONS.values()} == {"golden"}
