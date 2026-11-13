# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Piece 14 (agent review): generated golden and error chain event tests, each shown red by a named mutation (OMN-22974).

Plan section 3.2 and W19. Piece 14's chain is the review dispatch's command and terminal: a review command arrives on the
contract's command topic, the dispatch calls the roster's rungs in the overlay's order, and the command ends in exactly
one terminal event on one of the contract's three terminal topics (completed, refused or failed), all under the
command's id.

The scenarios are generated from ``node_agent_review_dispatch_orchestrator``'s ``contract.yaml`` and the node's own
enums: the command and terminal topics are read from the contract (``load_topics``), the outcome of each scenario
selects its terminal topic through the contract, and the reasons and rung outcomes are the node's enums, never spelled
in an assertion. Each scenario runs over a bus: the in-memory bus always, and a real broker of the declared test lane
when ``RUN_REDPANDA_PIECE14=1`` (on disposable topics tagged ``drill``). A backend that is asked for and cannot be
reached fails; nothing here skips.

The dispatch is the real handler with only its ports faked (the harness of the node's own tests); the bus carries the
command in and the terminal out, and the assertion reads what the bus delivered.

``NAMED_MUTATIONS`` records every injected break with the chain it turns red; ``-k mutation`` runs them. Each is run
against the unbroken scenario first (the control), so the red is the break's and not the scenario's.
"""

from __future__ import annotations

import asyncio
import copy
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import pytest
import yaml

from omnimarket.nodes.node_agent_review_dispatch_orchestrator.models import (
    EnumAgentReviewOutcome,
    EnumAgentReviewReason,
    EnumAgentRungOutcome,
    ModelAgentReviewRequest,
    ModelAgentReviewTerminal,
)
from omnimarket.nodes.node_agent_review_dispatch_orchestrator.overlay import (
    load_topics,
)
from tests.chains.piece16.test_piece16_chain import (
    MemoryChainBus,
    ProtocolChainBus,
    RedpandaChainBus,
)
from tests.integration.test_join_j14 import RECORDED_CREDIT_503, chain_overlay
from tests.unit.nodes.node_agent_review_dispatch_orchestrator.support import (
    HEAD,
    KEY,
    MOVED,
    Harness,
    World,
    build_harness,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
CONTRACT: dict[str, Any] = yaml.safe_load(
    (
        REPO
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_agent_review_dispatch_orchestrator"
        / "contract.yaml"
    ).read_text(encoding="utf-8")
)
TOPICS = load_topics()
REDPANDA_FLAG = "RUN_REDPANDA_PIECE14"
LAB_CLASS = "lab_local"
CONNECTION_CAUSE = "ConnectionError"

OUT = EnumAgentReviewOutcome
R = EnumAgentReviewReason
A = EnumAgentRungOutcome


BACKENDS = ["memory"] + (["redpanda"] if os.environ.get(REDPANDA_FLAG) == "1" else [])


@pytest.fixture(params=BACKENDS)
def bus(request: pytest.FixtureRequest) -> ProtocolChainBus:
    return MemoryChainBus() if request.param == "memory" else RedpandaChainBus()


# --- scenarios, generated from the contract -----------------------------------------------------------------------


@dataclass(frozen=True)
class Expect:
    outcome: EnumAgentReviewOutcome
    reason: EnumAgentReviewReason | None = None
    #: Identities on the signed receipt; empty means no receipt is signed, stored or posted.
    voters: tuple[str, ...] = ()
    #: (rung, identity, outcome, cause) of every voter call, in the terminal's order.
    attempts: tuple[tuple[str, str, EnumAgentRungOutcome, str], ...] = ()
    lab_calls: tuple[str, ...] = ()


@dataclass(frozen=True)
class Scenario:
    case_id: str
    kind: str  # "golden" or "error"
    order: tuple[str, ...]
    expect: Expect
    world: Callable[[], World] = World
    overlay: dict[str, Any] = field(default_factory=dict)


def generate_scenarios() -> tuple[Scenario, ...]:
    """One golden chain and the error faults of the dispatch, with expectations taken from the contract and enums."""
    return (
        Scenario(
            "lab-rung-answers",
            "golden",
            ("lab", "harness"),
            Expect(
                OUT.COMPLETED,
                voters=("lab-a", "lab-b"),
                attempts=(
                    ("lab", "lab-a", A.ANSWERED, ""),
                    ("lab", "lab-b", A.ANSWERED, ""),
                ),
                lab_calls=("lab-a", "lab-b"),
            ),
        ),
        Scenario(
            "unreachable-first-rung-then-lab-answers",
            "error",
            ("harness", "lab"),
            Expect(
                OUT.COMPLETED,
                voters=("lab-a", "lab-b"),
                attempts=(
                    ("harness", "harness-a", A.UNAVAILABLE, CONNECTION_CAUSE),
                    ("lab", "lab-a", A.ANSWERED, ""),
                    ("lab", "lab-b", A.ANSWERED, ""),
                ),
                lab_calls=("harness-a", "lab-a", "lab-b"),
            ),
            world=lambda: World(
                lab_raises={"harness-a"},
                lab_error_text={"harness-a": RECORDED_CREDIT_503},
            ),
        ),
        Scenario(
            "no-rung-can-answer",
            "error",
            ("lab", "harness"),
            Expect(
                OUT.REFUSED,
                reason=R.LAB_RUNG_INSUFFICIENT,
                attempts=(
                    ("lab", "lab-a", A.UNAVAILABLE, CONNECTION_CAUSE),
                    ("lab", "lab-b", A.UNAVAILABLE, CONNECTION_CAUSE),
                    ("harness", "harness-a", A.UNAVAILABLE, CONNECTION_CAUSE),
                ),
                lab_calls=("lab-a", "lab-b", "harness-a"),
            ),
            world=lambda: World(
                lab_raises={"lab-a", "lab-b", "harness-a"},
                lab_error_text={"lab-a": RECORDED_CREDIT_503},
            ),
        ),
        Scenario(
            "stale-head",
            "error",
            ("lab", "harness"),
            Expect(OUT.REFUSED, reason=R.STALE_HEAD),
            world=lambda: World(heads=[MOVED]),
        ),
        Scenario(
            "deadline-exceeded",
            "error",
            ("lab", "harness"),
            Expect(
                OUT.FAILED,
                reason=R.DEADLINE_EXCEEDED,
                lab_calls=("lab-a", "lab-b"),
            ),
            world=lambda: World(lab_delay=1.0),
            overlay={"deadline_seconds": 0.05, "lab_call_timeout_seconds": 2.0},
        ),
    )


SCENARIOS = {s.case_id: s for s in generate_scenarios()}


def test_scenarios_are_generated_from_the_contract_and_cover_both_kinds_and_every_terminal() -> (
    None
):
    terminals = CONTRACT["runtime_dispatch"]["terminal_events"]
    assert (TOPICS.completed, TOPICS.refused, TOPICS.failed) == (
        terminals["success"],
        terminals["refused"],
        terminals["failed"],
    )
    assert TOPICS.command == CONTRACT["runtime_dispatch"]["command_topic"]
    assert len({TOPICS.command, TOPICS.completed, TOPICS.refused, TOPICS.failed}) == 4
    assert {s.kind for s in SCENARIOS.values()} == {"golden", "error"}
    # Every terminal topic of the contract is reached by a scenario, and every outcome has its topic.
    assert {s.expect.outcome for s in SCENARIOS.values()} == set(OUT)
    assert {TOPICS.terminal_topic(o) for o in OUT} == set(terminals.values())


# --- the chain: a command in over the bus, the dispatch, the terminal out over the bus ----------------------------


@dataclass
class ChainRun:
    scenario: Scenario
    harness: Harness
    request: ModelAgentReviewRequest
    #: (topic the terminal was published on, payload the bus delivered)
    events: list[tuple[str, bytes]]


class Dispatch(Protocol):
    async def handle(
        self, request: ModelAgentReviewRequest
    ) -> ModelAgentReviewTerminal: ...


def build(
    case_id: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Scenario, Harness]:
    scenario = SCENARIOS[case_id]
    overlay = copy.deepcopy(chain_overlay(scenario.order))
    overlay.update(scenario.overlay)
    harness = build_harness(
        "inmemory", monkeypatch, tmp_path, overlay=overlay, world=scenario.world()
    )
    return scenario, harness


async def drive(
    bus: ProtocolChainBus,
    scenario: Scenario,
    harness: Harness,
    *,
    dispatch: Dispatch | None = None,
    publish_terminal: bool = True,
    topic_for: Callable[[ModelAgentReviewTerminal], str] | None = None,
) -> ChainRun:
    """Publish the command, run the dispatch on what the bus delivered, publish its terminal; return what came out."""
    request = harness.request()
    delivered = await bus.roundtrip(TOPICS.command, request.model_dump_json().encode())
    terminal = await (dispatch or harness.handler).handle(
        ModelAgentReviewRequest.model_validate_json(delivered)
    )
    events: list[tuple[str, bytes]] = []
    if publish_terminal:
        topic = (topic_for or (lambda t: TOPICS.terminal_topic(t.outcome)))(terminal)
        events.append(
            (topic, await bus.roundtrip(topic, terminal.model_dump_json().encode()))
        )
    return ChainRun(scenario, harness, request, events)


def assert_chain(run: ChainRun) -> None:
    """The one assertion of both kinds: exactly one terminal on the contract's topic, with the scenario's answer."""
    expect = run.scenario.expect
    world = run.harness.world
    assert len(run.events) == 1, f"{len(run.events)} terminal events, expected 1"
    topic, raw = run.events[0]
    terminal = ModelAgentReviewTerminal.model_validate_json(raw)
    assert topic == TOPICS.terminal_topic(expect.outcome)
    assert terminal.command_id == run.request.command_id
    assert terminal.outcome is expect.outcome
    assert terminal.reason is expect.reason
    assert [
        (a.rung, a.identity, a.outcome, a.cause) for a in terminal.attempts
    ] == list(expect.attempts)
    assert sorted(world.lab_calls) == sorted(expect.lab_calls)
    if expect.voters:
        receipt = world.stored[KEY]
        assert terminal.receipt_id == receipt.id
        assert receipt.signature
        assert receipt.head_sha == HEAD
        assert tuple(v.identity for v in receipt.voters) == expect.voters
        assert all(v.delegation_event_id for v in receipt.voters)
        assert world.posts == [receipt.id]
        # A rung that did not answer is typed on the terminal and never among the votes.
        answered = {a.identity for a in terminal.attempts if a.outcome is A.ANSWERED}
        assert {v.identity for v in receipt.voters} == answered
    else:
        assert terminal.receipt_id is None
        assert world.sign_calls == 0
        assert world.stored == {}
        assert world.posts == []


def run_scenario(
    bus: ProtocolChainBus,
    case_id: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    **options: Any,
) -> None:
    scenario, harness = build(case_id, monkeypatch, tmp_path)
    if "wrap" in options:
        options["dispatch"] = options.pop("wrap")(harness.handler)
    assert_chain(asyncio.run(drive(bus, scenario, harness, **options)))


def _ids(kind: str) -> list[str]:
    return [c for c, s in SCENARIOS.items() if s.kind == kind]


@pytest.mark.parametrize("case_id", _ids("golden"))
def test_golden_chain_agent_review_dispatch(
    bus: ProtocolChainBus,
    case_id: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_scenario(bus, case_id, monkeypatch, tmp_path)


@pytest.mark.parametrize("case_id", _ids("error"))
def test_error_chain_agent_review_dispatch(
    bus: ProtocolChainBus,
    case_id: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_scenario(bus, case_id, monkeypatch, tmp_path)


# --- the named mutations ------------------------------------------------------------------------------------------


class _Wrapped:
    """An injected break of the dispatch: the real handler's terminal, altered on its way out."""

    def __init__(
        self,
        inner: Dispatch,
        change: Callable[[ModelAgentReviewTerminal], ModelAgentReviewTerminal],
    ) -> None:
        self.inner = inner
        self.change = change

    async def handle(
        self, request: ModelAgentReviewRequest
    ) -> ModelAgentReviewTerminal:
        return self.change(await self.inner.handle(request))


def _alter(
    **update: Any,
) -> Callable[[Dispatch], Dispatch]:
    return lambda inner: _Wrapped(inner, lambda t: t.model_copy(update=update))


@dataclass(frozen=True)
class Mutation:
    kind: str  # the chain assertion it turns red: "golden" or "error"
    case_id: str
    options: dict[str, Any]
    note: str


MUTATIONS: dict[str, Mutation] = {
    "completed-terminal-not-published": Mutation(
        "golden",
        "lab-rung-answers",
        {"publish_terminal": False},
        "the receipt is posted and the completed terminal never reaches the bus",
    ),
    "completed-published-on-the-refused-topic": Mutation(
        "golden",
        "lab-rung-answers",
        {"topic_for": lambda _terminal: TOPICS.refused},
        "a completed review is announced on the refused topic",
    ),
    "completed-terminal-without-its-receipt-id": Mutation(
        "golden",
        "lab-rung-answers",
        {"wrap": _alter(receipt_id=None)},
        "the terminal completes without naming the receipt it posted",
    ),
    "unreachable-rung-not-typed-on-the-terminal": Mutation(
        "error",
        "unreachable-first-rung-then-lab-answers",
        {"wrap": _alter(attempts=())},
        "an unreachable rung leaves no typed attempt on the terminal",
    ),
    "refusal-terminal-not-published": Mutation(
        "error",
        "no-rung-can-answer",
        {"publish_terminal": False},
        "a chain with no rung able to answer ends silently",
    ),
    "refusal-published-as-completed": Mutation(
        "error",
        "no-rung-can-answer",
        {"wrap": _alter(outcome=OUT.COMPLETED, reason=None)},
        "a refused review is announced as completed",
    ),
    "stale-head-refusal-published-as-failed": Mutation(
        "error",
        "stale-head",
        {"wrap": _alter(outcome=OUT.FAILED)},
        "a stale head refusal is announced on the failed topic",
    ),
    "deadline-failure-published-as-refused": Mutation(
        "error",
        "deadline-exceeded",
        {"wrap": _alter(outcome=OUT.REFUSED)},
        "a deadline failure is announced on the refused topic",
    ),
}

#: The named mutations, each with the chain assertion it turns red and the test that shows it.
NAMED_MUTATIONS: dict[str, tuple[str, str]] = {
    name: (m.kind, f"test_mutation_turns_the_{m.kind}_chain_red[{name}]")
    for name, m in MUTATIONS.items()
}


def _mutation_ids(kind: str) -> list[str]:
    return [n for n, m in MUTATIONS.items() if m.kind == kind]


def _turns_red(
    bus: ProtocolChainBus,
    name: str,
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mutation = MUTATIONS[name]
    assert SCENARIOS[mutation.case_id].kind == kind
    # The control: the same scenario unbroken passes, so the red below is the break's.
    run_scenario(bus, mutation.case_id, monkeypatch, tmp_path / "control")
    with pytest.raises(AssertionError):
        run_scenario(
            bus,
            mutation.case_id,
            monkeypatch,
            tmp_path / "broken",
            **copy.copy(mutation.options),
        )


@pytest.mark.parametrize("name", _mutation_ids("golden"))
def test_mutation_turns_the_golden_chain_red(
    bus: ProtocolChainBus,
    name: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _turns_red(bus, name, "golden", monkeypatch, tmp_path)


@pytest.mark.parametrize("name", _mutation_ids("error"))
def test_mutation_turns_the_error_chain_red(
    bus: ProtocolChainBus,
    name: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _turns_red(bus, name, "error", monkeypatch, tmp_path)


def test_mutation_record_names_a_golden_and_an_error_break() -> None:
    assert {m.kind for m in MUTATIONS.values()} == {"golden", "error"}
    assert all(
        _mutation_ids(kind) and name in NAMED_MUTATIONS
        for kind in ("golden", "error")
        for name in _mutation_ids(kind)
    )


def test_mutation_names_are_unique_and_each_one_has_a_note() -> None:
    assert len(NAMED_MUTATIONS) == len(MUTATIONS)
    assert all(m.note for m in MUTATIONS.values())
    # Every error scenario has a break that turns it red.
    assert {m.case_id for m in MUTATIONS.values() if m.kind == "error"} == set(
        _ids("error")
    )
