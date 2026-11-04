# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The chain of responders: rungs in the overlay's order, escalation on measured failure.

RULING 2026-11-03T20:42:53Z: reviews run as one ordered chain of responders, like the
delegation chain. The first rung is called alone; a rung that answers unavailable is
skipped and recorded, never a red; a higher rung is called only when the rungs before it
left the quorum short. The error texts below are the recorded gateway 503 replies of the
failed reviewer runs (a credit-exhausted gateway key and a gateway with no key).
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_agent_review_dispatch_orchestrator.models import (
    EnumAgentReviewOutcome,
    EnumAgentReviewReason,
    EnumAgentRungOutcome,
    ModelAgentFinding,
    ModelAgentReviewConfig,
)
from omnimarket.nodes.node_agent_review_dispatch_orchestrator.overlay import (
    load_agent_review_config,
)
from tests.node_overlay_support import install_node_overlay

from .support import HEAD, KEY, NODE, OVERLAY, Harness, World, build_harness

pytestmark = pytest.mark.unit

#: The gateway replies of the recorded 503 runs, as the lab call renders them.
RECORDED_CREDIT_503 = (
    "[ONEX_CORE_205_SERVICE_UNAVAILABLE] Server error (503) from review-gateway"
    ' :: response_body={"error":{"type":"gateway_keys_set_aside",'
    '"reason":"credit_exhausted","message":"every key was refused"}}'
)
RECORDED_NO_KEY_503 = (
    "[ONEX_CORE_205_SERVICE_UNAVAILABLE] Server error (503) from review-gateway"
    ' :: response_body={"error":{"type":"gateway_no_key","message":"no key"}}'
)


def _voter(identity: str, rung: str, host: int, model: str) -> dict[str, str]:
    return {
        "identity": identity,
        "provider": "test-provider",
        "rung": rung,
        "endpoint_class": "test_class",
        "endpoint": f"192.0.2.{host}:8000",
        "model": model,
        "route_key": f"route-{identity}",
    }


def chain_overlay(
    order: tuple[str, ...] = ("first", "second", "third"),
) -> dict[str, Any]:
    """Four voters on three rungs: first has two, second and third one each."""
    fields = copy.deepcopy(OVERLAY)
    fields["roster"] = [
        _voter("first-a", "first", 21, "model-1"),
        _voter("first-b", "first", 22, "model-2"),
        _voter("second-a", "second", 23, "model-3"),
        _voter("third-a", "third", 24, "model-4"),
    ]
    fields["rung_order"] = list(order)
    fields["policy"]["approved_identities"] = [v["identity"] for v in fields["roster"]]
    fields["policy"]["approved_rungs"] = ["first", "second", "third"]
    fields["policy"]["forbidden_providers"] = []
    fields["lab_call_timeout_seconds"] = 0.05
    return fields


@pytest.fixture
def make(profile: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    def _make(
        order: tuple[str, ...] = ("first", "second", "third"), **kwargs: Any
    ) -> Harness:
        return build_harness(
            profile, monkeypatch, tmp_path, overlay=chain_overlay(order), **kwargs
        )

    return _make


async def test_the_first_rung_is_called_alone_and_answers_the_quorum(make: Any) -> None:
    h: Harness = make()
    terminal = await h.run(h.request())
    assert terminal.outcome is EnumAgentReviewOutcome.COMPLETED
    assert sorted(h.world.lab_calls) == ["first-a", "first-b"]
    assert [a.rung for a in terminal.attempts] == ["first", "first"]
    assert all(a.outcome is EnumAgentRungOutcome.ANSWERED for a in terminal.attempts)
    assert all(a.latency_seconds > 0 for a in terminal.attempts)


async def test_the_overlay_decides_which_rung_is_first(make: Any) -> None:
    h: Harness = make(("third", "second", "first"))
    terminal = await h.run(h.request())
    assert terminal.outcome is EnumAgentReviewOutcome.COMPLETED
    assert h.world.lab_calls == ["third-a", "second-a"]
    assert [a.rung for a in terminal.attempts] == ["third", "second"]


@pytest.mark.parametrize("recorded", [RECORDED_CREDIT_503, RECORDED_NO_KEY_503])
async def test_a_rung_lost_to_a_recorded_503_is_skipped_and_recorded_not_a_red(
    make: Any, recorded: str
) -> None:
    world = World(
        lab_raises={"first-a", "first-b"},
        lab_error_text={"first-a": recorded, "first-b": recorded},
    )
    h: Harness = make(world=world)
    terminal = await h.run(h.request())
    assert terminal.outcome is EnumAgentReviewOutcome.COMPLETED
    assert terminal.reason is None
    outcomes = [(a.identity, a.outcome) for a in terminal.attempts]
    assert outcomes == [
        ("first-a", EnumAgentRungOutcome.UNAVAILABLE),
        ("first-b", EnumAgentRungOutcome.UNAVAILABLE),
        ("second-a", EnumAgentRungOutcome.ANSWERED),
        ("third-a", EnumAgentRungOutcome.ANSWERED),
    ]
    assert all(a.cause == "ConnectionError" for a in terminal.attempts[:2])
    stored = world.stored[KEY]
    assert [v.identity for v in stored.voters] == ["second-a", "third-a"]
    assert world.posts == [stored.id]


async def test_a_rung_short_of_the_quorum_escalates_and_the_votes_add_up(
    make: Any,
) -> None:
    world = World(lab_raises={"first-b"})
    h: Harness = make(world=world)
    terminal = await h.run(h.request())
    assert terminal.outcome is EnumAgentReviewOutcome.COMPLETED
    assert world.lab_calls == ["first-a", "first-b", "second-a"]
    assert [v.identity for v in world.stored[KEY].voters] == ["first-a", "second-a"]


async def test_a_timed_out_rung_is_recorded_as_a_timeout(make: Any) -> None:
    world = World(lab_slow={"first-a", "first-b"})
    h: Harness = make(world=world)
    terminal = await h.run(h.request())
    assert terminal.outcome is EnumAgentReviewOutcome.COMPLETED
    assert [a.outcome for a in terminal.attempts[:2]] == [
        EnumAgentRungOutcome.TIMEOUT
    ] * 2


async def test_a_vote_without_a_delegation_event_is_unproven_and_escalates(
    make: Any,
) -> None:
    world = World(lab_empty_event={"first-a", "first-b"})
    h: Harness = make(world=world)
    terminal = await h.run(h.request())
    assert terminal.outcome is EnumAgentReviewOutcome.COMPLETED
    assert [a.outcome for a in terminal.attempts[:2]] == [
        EnumAgentRungOutcome.UNPROVEN
    ] * 2
    assert [v.identity for v in world.stored[KEY].voters] == ["second-a", "third-a"]


async def test_no_rung_able_to_answer_stays_not_passed_with_every_attempt_recorded(
    make: Any,
) -> None:
    world = World(
        lab_raises={"first-a", "first-b", "second-a", "third-a"},
        lab_error_text={
            "first-a": RECORDED_CREDIT_503,
            "first-b": RECORDED_NO_KEY_503,
        },
    )
    h: Harness = make(world=world)
    terminal = await h.run(h.request())
    assert terminal.outcome is EnumAgentReviewOutcome.REFUSED
    assert terminal.reason is EnumAgentReviewReason.LAB_RUNG_INSUFFICIENT
    assert [a.identity for a in terminal.attempts] == [
        "first-a",
        "first-b",
        "second-a",
        "third-a",
    ]
    assert all(a.outcome is EnumAgentRungOutcome.UNAVAILABLE for a in terminal.attempts)
    assert world.sign_calls == 0
    assert world.stored == {}
    assert world.posts == []
    assert h.world.terminals["cmd-1"].attempts == terminal.attempts


async def test_every_call_of_the_chain_is_pinned_to_the_requested_head(
    make: Any,
) -> None:
    world = World(lab_raises={"first-a"})
    h: Harness = make(world=world)
    await h.run(h.request())
    assert world.lab_heads == [HEAD] * len(world.lab_calls)
    assert len(world.lab_calls) == 3


async def test_two_rungs_raising_one_finding_still_block(make: Any) -> None:
    finding = ModelAgentFinding(path="a.py", line=10, severity="critical", claim="x")
    world = World(
        lab_raises={"first-b"},
        lab_findings={"first-a": (finding,), "second-a": (finding,)},
    )
    h: Harness = make(world=world)
    terminal = await h.run(h.request())
    assert terminal.outcome is EnumAgentReviewOutcome.COMPLETED
    stored = world.stored[KEY]
    assert stored.quorum_verdict == "blocked"
    assert len(stored.blocking) == 1


async def test_one_rung_raising_a_finding_alone_does_not_block_as_in_the_ci_gate(
    make: Any,
) -> None:
    finding = ModelAgentFinding(path="a.py", line=10, severity="critical", claim="x")
    world = World(lab_findings={"first-a": (finding,)})
    h: Harness = make(world=world)
    await h.run(h.request())
    stored = world.stored[KEY]
    assert stored.quorum_verdict == "passed"
    assert stored.findings_count == 1


async def test_a_head_with_a_stored_receipt_calls_no_rung(make: Any) -> None:
    h: Harness = make()
    first = await h.run(h.request("cmd-1"))
    calls = len(h.world.lab_calls)
    second = await h.run(h.request("cmd-2"))
    assert second.receipt_id == first.receipt_id
    assert len(h.world.lab_calls) == calls
    assert second.attempts == ()


@pytest.mark.parametrize(
    "order",
    [
        ("first", "second"),
        ("first", "second", "third", "third"),
        ("first", "second", "fourth"),
    ],
    ids=["rung-missing", "rung-repeated", "rung-without-voter"],
)
def test_an_order_that_differs_from_the_roster_rungs_is_refused_at_load(
    order: tuple[str, ...], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fields = chain_overlay(order)
    install_node_overlay(
        monkeypatch, tmp_path, NODE, {**fields, "transport": "in_memory"}
    )
    with pytest.raises(NodeOverlayError):
        load_agent_review_config()


def test_the_order_is_required_in_the_overlay() -> None:
    fields = chain_overlay()
    del fields["rung_order"]
    with pytest.raises(ValueError, match="rung_order"):
        ModelAgentReviewConfig.model_validate(fields)
