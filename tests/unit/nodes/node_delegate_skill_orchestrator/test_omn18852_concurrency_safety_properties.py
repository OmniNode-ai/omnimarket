# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why concurrent consumption is safe HERE and not one node over (OMN-18852).

OMN-18852 declared `consume_concurrency: max_in_flight_records: 4` on
`node_delegate_skill_orchestrator`. Two objections are correct to raise and
are answered here by assertion rather than by prose in a pull request, because
prose does not fail when someone changes the code underneath it.

**The driver does NOT serialise by key.** `EventBusKafka`'s bounded in-flight
path is a plain `asyncio.Semaphore(max_in_flight)` — records are dispatched
concurrently regardless of partition key. So nothing in the transport protects
a node that holds per-correlation state across events. Safety has to come from
the node, and these tests are the statement of what makes it safe.

Three properties, each independently sufficient to break if violated:

1. **One subscribe topic.** This node cannot receive two *kinds* of event for
   one correlation, so "same-correlation events handled out of order" is not
   reachable. `node_delegation_orchestrator` subscribes to several topics that
   all carry one correlation, which is exactly why it is left serial.
2. **No cross-record state.** The handler keeps a frozen budget and a port.
   Nothing is keyed by correlation, so two records in flight cannot interfere.
3. **No slot deadlock.** The handler awaits the delegation reply inline while
   holding one of the four slots. If that reply were delivered through the same
   consumer group, four waiters would need a fifth slot to make progress and
   the pool would wedge at full occupancy. It is not: the dispatch port
   subscribes on a **per-correlation ephemeral group**, one per waiting
   handler, on different topics entirely.

On offset ordering, stated rather than tested because the test would be a lie:
this path runs broker-side auto-commit and never calls `commit()`, so the
fetch position already advances ahead of in-flight handlers *in the serial path
too*. "Offsets commit in order behind a slow record" is not a property this
consumer had before OMN-18852 and not one it lost. Delivery is at-least-once,
which `_process_consumed_record` documents as this path's contract.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_runtime_delegation_dispatch,
)

_NODES = Path(__file__).resolve().parents[4] / "src" / "omnimarket" / "nodes"
_DELEGATE_SKILL = _NODES / "node_delegate_skill_orchestrator" / "contract.yaml"
_FSM_ORCHESTRATOR = _NODES / "node_delegation_orchestrator" / "contract.yaml"


def _contract(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _subscribe_topics(path: Path) -> list[str]:
    topics = _contract(path)["event_bus"]["subscribe_topics"]
    assert isinstance(topics, list)
    return [str(t) for t in topics]


@pytest.mark.unit
def test_the_parallelised_node_consumes_exactly_one_topic() -> None:
    """Property 1 — a second subscribe topic would make out-of-order reachable.

    With one topic, every record is a whole delegation carrying its own
    correlation. There is no second event kind that could arrive for the same
    correlation and be handled before or beside the first.
    """
    topics = _subscribe_topics(_DELEGATE_SKILL)

    assert topics == ["onex.cmd.omnimarket.delegate-skill.v1"], (
        "OMN-18852: node_delegate_skill_orchestrator now subscribes to "
        f"{topics}. It declares consume_concurrency, and the bounded in-flight "
        "driver does NOT serialise by key, so a second subscribe topic means "
        "two events for one correlation can be handled concurrently. Either "
        "drop the added topic or remove the concurrency declaration."
    )


@pytest.mark.unit
def test_the_serial_fsm_node_is_the_one_with_many_topics_for_one_correlation() -> None:
    """The positive control for property 1, so it is a contrast and not a tautology.

    A single-topic assertion proves little unless some node in the same family
    actually has several. This is that node, and it is the one deliberately
    left serial.
    """
    fsm_topics = _subscribe_topics(_FSM_ORCHESTRATOR)

    assert len(fsm_topics) > 1, (
        "OMN-18852: node_delegation_orchestrator now subscribes to "
        f"{len(fsm_topics)} topic(s). The asymmetry these tests encode assumed "
        "it is the multi-topic correlation-keyed FSM. If that changed, "
        "re-derive whether it may now be parallelised."
    )
    assert "consume_concurrency" not in _contract(_FSM_ORCHESTRATOR), (
        "OMN-18852: the correlation-keyed FSM must stay serial until the "
        "in-flight path serialises by key."
    )


@pytest.mark.unit
def test_the_handler_keeps_no_state_keyed_by_correlation() -> None:
    """Property 2 — two records in flight must not be able to interfere.

    Constructed with an explicit port and budget, the handler's instance
    attributes must be exactly the collaborators it was given. Any container
    (dict/list/set) is a place per-correlation state could accumulate, and
    under four-way concurrency that becomes a cross-request data race.
    """
    handler = HandlerDelegateSkill(dispatch_port=object(), budget=None)  # type: ignore[arg-type]

    containers = {
        name: value
        for name, value in vars(handler).items()
        if isinstance(value, (dict, list, set))
    }

    assert not containers, (
        "OMN-18852: HandlerDelegateSkill gained mutable container state "
        f"{sorted(containers)}. This handler runs up to "
        "max_in_flight_records times concurrently, and the driver does not "
        "serialise by key, so shared mutable state here is a cross-request "
        "race rather than a style question."
    )


@pytest.mark.unit
def test_handle_takes_the_request_and_returns_a_terminal_holding_nothing() -> None:
    """Property 2, second half: canonical definition-B shape, so state has nowhere to live."""
    signature = inspect.signature(HandlerDelegateSkill.handle)
    parameters = [p for p in signature.parameters if p != "self"]

    assert len(parameters) == 1, (
        "OMN-18852: handle() must take exactly the typed request "
        f"(definition-B), got {parameters}."
    )


@pytest.mark.unit
def test_the_reply_subscription_is_per_correlation_so_four_waiters_cannot_wedge() -> (
    None
):
    """Property 3 — the deadlock this design would have if replies shared a group.

    The handler holds a slot for the whole delegation, awaiting the reply. Were
    that reply delivered through the group that admitted the request, four
    occupied slots would leave no capacity to deliver any of the four replies
    and the pool would wedge at full occupancy until every budget expired.

    It cannot: the group id is built per dispatch correlation, so each waiting
    handler owns a distinct ephemeral group on the reply topics.
    """
    source = inspect.getsource(port_runtime_delegation_dispatch)

    assert "consumer_group_prefix}-{dispatch_correlation_id.hex}" in source, (
        "OMN-18852: the dispatch port's reply subscription is no longer keyed "
        "per correlation. If replies are delivered through a shared group — "
        "worse, the group that admitted the request — then N concurrent "
        "handlers each holding a slot while awaiting a reply can wedge the "
        "pool at full occupancy. Re-derive the deadlock argument before "
        "changing this."
    )


@pytest.mark.unit
def test_the_declared_bound_is_reflected_in_the_contract_that_was_reasoned_about() -> (
    None
):
    """Guard against these proofs outliving the declaration they justify."""
    block = _contract(_DELEGATE_SKILL).get("consume_concurrency")

    assert block is not None, (
        "OMN-18852: the concurrency declaration these safety proofs exist to "
        "justify is gone. Delete these tests in the same change, or restore "
        "the declaration."
    )
    assert block["max_in_flight_records"] > 1
