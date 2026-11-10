# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain (OMN-23611): the packaged contract resolves to the handler, a good request reads WORKING with the
topics the contract declares, and each named mutation turns the chain red.

Every mutation in NAMED_MUTATIONS replaces one rule in the handler module and runs the same golden assertions; the
mutated chain must raise AssertionError, which shows the assertion can fail.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.models.model_pilot_measurement import ModelPilotMeasurementResult
from omnimarket.models.model_reading import EnumReadingVerdict as V
from omnimarket.nodes.node_pilot_measurement_compute.handlers import (
    handler_pilot_measurement as module,
)
from tests.nodes.node_pilot_measurement_compute.pilot_support import (
    contract,
    declaration,
    fact,
    finished,
    request,
    resolve_handler,
)

#: mutation name -> (the chain it turns red, the test that shows it).
NAMED_MUTATIONS: dict[str, tuple[str, str]] = {
    "check-never-run": (
        "golden",
        "test_mutation_check_never_run_turns_the_golden_chain_red",
    ),
    "pending-counted-as-failure": (
        "golden",
        "test_mutation_pending_counted_as_failure_turns_the_golden_chain_red",
    ),
    "newest-fact-ignored": (
        "golden",
        "test_mutation_newest_fact_ignored_turns_the_golden_chain_red",
    ),
}


def run_golden_chain() -> None:
    """The golden assertions: contract routing, the good request, a pending pilot, the newest fact of a pilot."""
    bus = contract()["event_bus"]
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.pilot-measurement-read-requested.v1"
    ]
    assert bus["publish_topics"] == ["onex.evt.omnimarket.pilot-measurement-read.v1"]
    assert contract()["terminal_event"] in bus["publish_topics"]
    handler = resolve_handler()()
    reading = handler.handle(request())
    assert isinstance(reading, ModelPilotMeasurementResult)
    assert reading.verdict is V.WORKING
    assert reading.reasons == ()
    assert reading.accepted == ("memory",)
    pending = handler.handle(request(declarations=[declaration()], facts=[fact()]))
    assert pending.verdict is V.WORKING
    assert pending.pending == ("memory",)
    older = fact(
        bar_hash=None,
        started_at="2026-11-10T10:00:00Z",
        frozen_at="2026-11-10T09:00:00Z",
    )
    newest = handler.handle(request(facts=[older, finished()]))
    assert newest.verdict is V.WORKING


def test_contract_declares_a_pure_compute_node_with_one_operation() -> None:
    declared = contract()
    assert declared["name"] == "node_pilot_measurement_compute"
    assert declared["node_type"] == "compute"
    assert declared["descriptor"]["side_effects"] == []
    [entry] = declared["handler_routing"]["handlers"]
    assert declared["handler_routing"]["routing_strategy"] == "operation_match"
    assert entry["operation"] == "pilot_measurement"
    assert resolve_handler() is module.HandlerPilotMeasurement


def test_golden_chain_good_request_reads_working_through_the_contract_handler() -> None:
    run_golden_chain()


def test_mutation_check_never_run_turns_the_golden_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "judge_pilot", lambda *_args, **_kw: None)
    with pytest.raises(AssertionError):
        run_golden_chain()


def test_mutation_pending_counted_as_failure_turns_the_golden_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real: Any = module.judge_pilot

    def mutated(
        checker: Any, declaration_: Any, fact_: Any, _elapsed: float, tally: Any
    ) -> None:
        real(
            checker,
            declaration_,
            fact_,
            float(declaration_.result_deadline_seconds) + 1,
            tally,
        )

    monkeypatch.setattr(module, "judge_pilot", mutated)
    with pytest.raises(AssertionError):
        run_golden_chain()


def test_mutation_newest_fact_ignored_turns_the_golden_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def oldest(req: Any) -> dict[str, Any]:
        return {
            f.pilot_id: f
            for f in sorted(req.facts, key=lambda f: f.started_at, reverse=True)
        }

    monkeypatch.setattr(module, "newest_facts", oldest)
    with pytest.raises(AssertionError):
        run_golden_chain()


def test_the_golden_chain_is_green_again_after_each_mutation_is_undone() -> None:
    """Positive control: the chain the mutations break is the one that passes unmutated."""
    run_golden_chain()


def test_every_named_golden_mutation_has_its_test() -> None:
    names = {n for n, v in globals().items() if n.startswith("test_") and callable(v)}
    assert {test for _, test in NAMED_MUTATIONS.values()} <= names
    assert {kind for kind, _ in NAMED_MUTATIONS.values()} == {"golden"}
