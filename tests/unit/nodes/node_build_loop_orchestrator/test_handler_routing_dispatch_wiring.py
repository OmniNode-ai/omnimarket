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
    operations = [h.operation for h in contract.handler_routing.handlers]

    assert operations == ["build_loop_orchestrator"], (
        "node_build_loop_orchestrator/contract.yaml must declare exactly one "
        f"handler_routing entry; found {operations!r}. Extra untyped entries "
        "previously hijacked the start-topic dispatch (OMN-15002) -- if this "
        "assertion is failing because a new entry was added intentionally, "
        "that entry MUST declare an explicit `topic:` or `event_type:` so "
        "route assignment stays deterministic."
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

    (real_entry,) = contract.handler_routing.handlers
    assert real_entry.operation == "build_loop_orchestrator"

    assigned = _topics_for_handler_entry(contract, real_entry)

    assert _TOPIC_START in assigned, (
        "HandlerBuildLoopOrchestrator's contract entry does not own the "
        f"start topic (assigned={assigned!r}) -- the daily build-loop "
        "trigger would be silently lost again (OMN-15002/OMN-15001 finding 1)."
    )
    assert _TOPIC_OVERSEER_COMPLETED in assigned


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
