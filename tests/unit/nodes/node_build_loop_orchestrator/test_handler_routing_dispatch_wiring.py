# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression test for OMN-15002 (parent OMN-15001 finding 1).

``node_build_loop_orchestrator``'s daily trigger was a 100% silent no-op for
6+ consecutive days. Root cause: the contract declared 3 dead
``handler_routing`` entries (``linear_fill``/``AdapterLinearFill``,
``llm_classify``/``AdapterLlmClassify``, ``llm_dispatch``/``AdapterLlmDispatch``)
alongside the real ``build_loop_orchestrator`` entry. None of those 3 dead
entries declared an ``event_model``, so omnibase_infra's auto-wiring
(``_topics_for_handler_entry``) granted each of them EVERY contract
``subscribe_topic`` unconditionally (the pre-existing single-handler
disambiguation rule only special-cased truly-sole handlers, and the "no
event_model" branch bypassed that check entirely) -- while the real,
``event_model``-bearing ``build_loop_orchestrator`` entry fell into the
multi-handler/multi-topic ambiguity guard and got ZERO routes. Every live
dispatch fanned out to the 3 dead adapters (which threw
``ValidationError``/``TypeError`` on the mismatched payload) while
``HandlerBuildLoopOrchestrator.handle()`` -- the only real dispatcher --
never ran.

This test drives the REAL ``omnibase_infra`` route-assignment function
(``_topics_for_handler_entry``) against the REAL on-disk contract -- not a
mock or a synthetic fixture -- so a future regression (re-adding an
ambiguous untyped handler entry, or losing the sole-handler property) fails
this test the same way it failed in production, rather than only failing a
happy-path contract-shape check.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_infra.runtime.auto_wiring.discovery import _parse_contract
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _topics_for_handler_entry,
)
from omnibase_infra.runtime.auto_wiring.models import ModelDiscoveredContract

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CONTRACT_PATH = (
    _REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_build_loop_orchestrator"
    / "contract.yaml"
)

_TOPIC_START = "onex.cmd.omnimarket.build-loop-orchestrator-start.v1"
_TOPIC_OVERSEER_COMPLETED = "onex.evt.omnimarket.overseer-verifier-completed.v1"


def _load_contract() -> ModelDiscoveredContract:
    return _parse_contract(
        contract_path=_CONTRACT_PATH,
        entry_point_name="node_build_loop_orchestrator",
        package_name="omnimarket",
        package_version="0.0.0",
    )


@pytest.mark.unit
def test_build_loop_orchestrator_is_the_sole_handler_routing_entry() -> None:
    """Regression guard: no second handler_routing entry may compete for
    this contract's subscribe topics.

    This is the structural precondition the fix relies on -- as long as
    ``build_loop_orchestrator`` is the ONLY entry, omnibase_infra's
    single-handler rule unambiguously assigns it every subscribe topic. Any
    future PR that adds a second ``handler_routing`` entry to this contract
    (e.g. resurrecting a composition-only adapter as an independent Kafka
    consumer) must also give it an explicit ``topic``/``event_type`` so it
    cannot silently re-create the OMN-15002 collision.
    """
    contract = _load_contract()
    assert contract.handler_routing is not None
    handlers = list(contract.handler_routing.handlers)

    # OMN-18013 took the branch this test's own docstring names. The contract now
    # declares ONE entry PER TOPIC, each carrying an explicit `topic:` AND an explicit
    # `message_category:` -- because a single entry spanning the .cmd. start topic and
    # the .evt. overseer fact registers BOTH under subscribe_topics[0]'s category, and
    # the event topic then matches zero routes at dispatch time.
    #
    # So the invariant is no longer "exactly one entry". It is the property that made
    # one entry safe in the first place: no entry may be untyped, because an untyped
    # entry is granted EVERY subscribe topic by _topics_for_handler_entry and that is
    # what produced the OMN-15002 collision. An untyped entry is refused here whether
    # it is the second or the twentieth.
    untyped = [
        h.operation
        for h in handlers
        if getattr(h, "topic", None) is None and getattr(h, "event_type", None) is None
    ]
    assert untyped == [], (
        "node_build_loop_orchestrator/contract.yaml declares handler_routing entry/ies "
        f"with neither `topic:` nor `event_type:`: {untyped!r}. _topics_for_handler_entry "
        "grants such an entry EVERY subscribe topic unconditionally, which is exactly how "
        "the daily trigger was silently lost for six days (OMN-15002 / OMN-15001)."
    )

    owners: dict[str, list[str]] = {}
    for handler in handlers:
        owners.setdefault(str(handler.topic), []).append(handler.operation)
    contested = {topic: ops for topic, ops in owners.items() if len(ops) > 1}
    assert contested == {}, (
        f"two handler_routing entries claim the same topic: {contested!r}. Route "
        "assignment must stay deterministic."
    )

    assert set(owners) == {_TOPIC_START, _TOPIC_OVERSEER_COMPLETED}, (
        f"the per-topic entries and the subscribe list have diverged: {sorted(owners)}"
    )


@pytest.mark.unit
def test_real_handler_entry_owns_the_start_topic_via_live_auto_wiring() -> None:
    """Drive the REAL omnibase_infra route-assignment function against the
    real on-disk contract and assert the live handler owns the start topic.

    This is the exact function (``_topics_for_handler_entry``) that produced
    an empty route set for the real handler and a full route set for the 3
    dead adapters in production. Asserting its output directly against the
    live contract is the failure-path proof: before the OMN-15002 fix this
    assertion failed with ``assigned == ()`` for the real handler.
    """
    contract = _load_contract()
    assert contract.handler_routing is not None
    assert contract.event_bus is not None

    entries = list(contract.handler_routing.handlers)
    assert {h.operation for h in entries} == {"build_loop_orchestrator"}

    # Drive the REAL assignment function once per entry and union the result. Under
    # OMN-18013 each entry owns exactly its own topic, so the union is the proof the
    # start topic is still reachable AND the proof no entry is hoovering up a topic it
    # does not declare -- the two halves of the OMN-15002 failure, checked together.
    assigned_by_entry = {
        str(entry.topic): _topics_for_handler_entry(contract, entry)
        for entry in entries
    }
    for topic, assigned in assigned_by_entry.items():
        assert assigned == (topic,), (
            f"entry for {topic} was assigned {assigned!r} rather than exactly its own "
            "topic — an over-broad assignment is the OMN-15002 shape"
        )

    union = {topic for assigned in assigned_by_entry.values() for topic in assigned}
    assert _TOPIC_START in union, (
        "HandlerBuildLoopOrchestrator's contract entries do not own the "
        f"start topic (assigned={assigned_by_entry!r}) -- the daily build-loop "
        "trigger would be silently lost again (OMN-15002/OMN-15001 finding 1)."
    )
    assert _TOPIC_OVERSEER_COMPLETED in union


@pytest.mark.unit
def test_dead_adapter_operations_are_not_declared_in_the_contract() -> None:
    """The 3 dead composition-only adapters must never reappear as
    independent handler_routing entries.

    ``AdapterLinearFill``/``AdapterLlmClassify``/``AdapterLlmDispatch`` are
    legacy classes consumed only by the non-canonical ``assemble_live.py``
    script (constructed directly in Python). The live DI path
    (``HandlerBuildLoopOrchestrator._ensure_sub_handlers``) always uses the
    real sub-handler implementations declared under
    ``sub_handler_dependencies``. Declaring these adapters as
    ``handler_routing`` entries with no ``event_model`` is what caused them
    to be auto-wired as independent (and silently failing) Kafka consumers.
    """
    contract = _load_contract()
    assert contract.handler_routing is not None
    operations = {h.operation for h in contract.handler_routing.handlers}

    dead_operations = {"linear_fill", "llm_classify", "llm_dispatch"}
    assert operations.isdisjoint(dead_operations), (
        f"Dead adapter operations reappeared in handler_routing: "
        f"{operations & dead_operations!r}. See OMN-15002 for why these "
        "must never be independent handler_routing entries."
    )
