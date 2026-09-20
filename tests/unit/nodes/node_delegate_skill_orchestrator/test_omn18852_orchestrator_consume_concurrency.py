# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegate-skill orchestrator is the lane's single-flight point (OMN-18852).

Raising a concurrency bound DOWNSTREAM of a single-flight stage buys nothing.
That is not a design opinion here, it is a measurement.

On 2026-09-20T11:28Z, with `node_llm_delegation_call_effect` already declaring
`max_in_flight_records: 4` and the runtime having logged the wiring for both of
its topics, four concurrent wrapper delegates still ran strictly serially:

* peak `vllm:num_requests_running` was **1.0** across 70 one-second samples;
* inference windows from the effects log were [11:28:39-43], [11:28:46-50],
  [11:28:54-56], [11:29:00-07] -- **zero overlapping pairs**, 3-4s apart;
* yet all four commands were published within the same second.

The inference consumer was allowed four in flight and never had more than one,
because records were not arriving concurrently. `HandlerDelegateSkill.handle()`
awaits the whole delegation inline under `handler_execution_budget`, so at the
default of one in-flight record exactly one delegation exists at a time.

These tests pin the declaration and, just as importantly, pin the ASYMMETRY:
`node_delegation_orchestrator` is deliberately left serial because it is a
correlation-keyed FSM. A future edit that "finishes the job" by adding the key
there would trade a throughput problem for a correctness one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_NODES = Path(__file__).resolve().parents[4] / "src" / "omnimarket" / "nodes"

_DELEGATE_SKILL = _NODES / "node_delegate_skill_orchestrator" / "contract.yaml"
_INFERENCE_EFFECT = _NODES / "node_llm_delegation_call_effect" / "contract.yaml"
_FSM_ORCHESTRATOR = _NODES / "node_delegation_orchestrator" / "contract.yaml"

_KEY = "consume_concurrency"
_FIELD = "max_in_flight_records"


def _contract(path: Path) -> dict[str, Any]:
    assert path.is_file(), f"contract not found: {path}"
    loaded = yaml.safe_load(path.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _declared_bound(path: Path) -> int | None:
    block = _contract(path).get(_KEY)
    if block is None:
        return None
    assert isinstance(block, dict), f"{path.name}: {_KEY} must be a mapping"
    bound = block.get(_FIELD)
    assert isinstance(bound, int), f"{path.name}: {_FIELD} must be an int"
    return bound


@pytest.mark.unit
def test_delegate_skill_orchestrator_declares_a_bound_above_one() -> None:
    """The single-flight point must opt out of the serial consume path.

    RED before this change: the key was absent, so the loader returned the
    serial default of 1 and the measured zero-overlap result above stood.
    """
    bound = _declared_bound(_DELEGATE_SKILL)

    assert bound is not None, (
        "OMN-18852: node_delegate_skill_orchestrator declares no "
        f"`{_KEY}`, so it consumes at the serial default of 1 and awaits each "
        "whole delegation inline. Every downstream concurrency bound is then "
        "unreachable — measured 2026-09-20T11:28Z as four concurrent callers "
        "with zero overlapping inference windows."
    )
    assert bound > 1, (
        f"OMN-18852: a declared bound of {bound} is the serial path. The "
        "declaration exists to remove the single-flight stage, not to restate "
        "it."
    )


@pytest.mark.unit
def test_the_bound_does_not_exceed_what_the_inference_pool_can_absorb() -> None:
    """Admitting more delegations than the pool downstream can serve just moves the queue.

    The inference effect's bound is itself matched to the local engine's
    `--max-num-seqs`. Exceeding it here would queue at the inference topic
    instead of here — the same wait, one hop later, and harder to see.
    """
    upstream = _declared_bound(_DELEGATE_SKILL)
    downstream = _declared_bound(_INFERENCE_EFFECT)

    assert upstream is not None
    assert downstream is not None
    assert upstream <= downstream, (
        f"OMN-18852: the orchestrator admits {upstream} concurrent "
        f"delegations while the inference pool serves {downstream}. The "
        "surplus does not become throughput, it becomes queue depth one hop "
        "downstream where no caller can see it."
    )


@pytest.mark.unit
def test_the_correlation_keyed_fsm_is_deliberately_left_serial() -> None:
    """The asymmetry is a decision, and this is where it is enforced.

    `node_delegation_orchestrator` is a correlation-keyed FSM that self-loops
    (compliance repair emits a fresh inference intent and stays in ROUTED) and
    consumes several topics whose records refer to the same correlation.
    Concurrent records there could race one correlation's state — a
    correctness bug, not a throughput one. Parallelising it requires per-key
    serialisation first.

    If you are here because this test failed after you added the key there:
    that is this test doing its job. Add per-key serialisation, then change
    this test deliberately.
    """
    assert _declared_bound(_FSM_ORCHESTRATOR) is None, (
        "OMN-18852: node_delegation_orchestrator now declares "
        f"`{_KEY}`. It is a correlation-keyed FSM consuming several topics "
        "that refer to the same correlation, so concurrent records can race "
        "one correlation's state. It is also not the bottleneck: its "
        "per-record work is route-and-publish and does not await the "
        "provider. Removing its serialisation trades a throughput problem for "
        "a correctness one."
    )


@pytest.mark.unit
def test_the_bound_stays_within_the_handler_budget_and_poll_deadline() -> None:
    """The OMN-15504 livelock invariant must survive the concurrency change.

    Concurrent handlers run as tasks while the loop keeps polling, so the
    consumer reaches its next poll sooner than in the serial case. The budget
    must still be strictly below the port's wait, which is what OMN-15504
    established and what this re-asserts alongside the new key.
    """
    contract = _contract(_DELEGATE_SKILL)
    budget = contract["handler_execution_budget"]["max_handler_duration_seconds"]
    port_wait = contract["delegation_runtime_dispatch"]["wait_timeout_seconds"]

    assert budget < port_wait, (
        f"OMN-15504: the handler budget {budget}s must stay strictly below "
        f"the port's wait {port_wait}s. A bound equal to the thing it exists "
        "to pre-empt pre-empts nothing."
    )
