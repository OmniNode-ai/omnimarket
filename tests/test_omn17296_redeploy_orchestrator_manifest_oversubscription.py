# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_redeploy_orchestrator must not declare a subscription it cannot service (OMN-17296).

THE DEFECT
----------
``node_redeploy_orchestrator`` declared
``onex.evt.omnibase-infra.runtime-manifest-published.v1`` in ``event_bus.subscribe_topics``
and had no way to receive it. Live on the .201 dev lane 2026-08-31, consumer group
``local.omnimarket.node_redeploy_orchestrator.consume.1.0.0.__i.runtime-effects.__t.onex.evt.omnibase-infra.runtime-manifest-published.v1``
joined, took 189 messages and routed 189 to the DLQ with
``failure_class=no_dispatcher`` — then committed each offset, so the group read
Stable / LAG 0 while 100% of that topic's traffic was lost.

Mechanism (the OMN-14605 shape the OMN-16939 ratchet already records for this contract):
the contract's ONE ``operation_match`` handler entry declares no ``message_category``, so
``derive_entry_message_category`` falls back to ``subscribe_topics[0]`` —
``onex.cmd.omnimarket.redeploy-start.v1``, a COMMAND — and stamps ``command`` on every
route the entry registers, including the eight ``onex.evt.*`` topics.
``MessageDispatchEngine`` filters on the topic's own real category before any handler
runs, so an ``event`` can never reach a route stamped ``command``.

WHY THE DISPOSITION IS "DROP", NOT "CONVERT TO topic_match"
-----------------------------------------------------------
OMN-17296 AC2 allows exactly two dispositions and no third state. Converting is the wrong
one here, and ``test_manifest_event_has_no_branch_in_the_real_handler`` proves it with the
real artifact: ``HandlerRedeployOrchestrator.handle`` branches on
``prod-promotion-grant-resolved`` / ``prod-promotion-gate-evaluated`` /
``runtime-image-built`` and defaults everything else to the redeploy-START path, which
coerces the payload into ``ModelRedeployStartCommand``. A manifest event therefore does
not reach any legitimate branch — routing it would swap a DLQ drop for a handler crash on
the deploy-start path. The subscription is vestigial: it dates from the pre-OMN-13211
in-process ``node_redeploy`` FSM (the ``PROBING`` phase of OMN-12577), which the canonical
bus-dispatching orchestrator replaced without ever implementing a manifest branch.

The gate applied here is the SHIPPED OMN-16939 validator
(``omnibase_infra.validators.subscriber_dispatcher_resolution``), driven through the real
discovery path — not a re-implementation of its rules, which is how this class survived
three earlier gates.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.validators.subscriber_dispatcher_resolution import (
    REASON_CATEGORY_MISMATCH,
    unresolved_subscriptions,
)
from pydantic import ValidationError

from omnimarket.nodes.node_redeploy_orchestrator.handlers.handler_redeploy_orchestrator import (
    HandlerRedeployOrchestrator,
)

MANIFEST_TOPIC = "onex.evt.omnibase-infra.runtime-manifest-published.v1"
CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_redeploy_orchestrator"
    / "contract.yaml"
)


def _contract() -> Any:
    discovered = discover_contracts_from_paths([CONTRACT_PATH])
    contracts = list(getattr(discovered, "contracts", discovered))
    assert len(contracts) == 1, f"expected 1 discovered contract, got {len(contracts)}"
    return contracts[0]


def test_manifest_topic_is_not_declared_as_a_subscription() -> None:
    """AC2: the disposition is DROP, so the topic must be gone from the contract."""
    contract = _contract()
    assert contract.event_bus is not None
    assert MANIFEST_TOPIC not in contract.event_bus.subscribe_topics, (
        "node_redeploy_orchestrator has no branch for runtime-manifest-published; a "
        "declared-but-unserviceable subscription is a false statement in the contract "
        "graph — the topic reads as consumed while every message is DLQ'd and committed"
    )


def test_no_subscription_of_this_contract_is_unresolvable_on_that_topic() -> None:
    """The shipped OMN-16939 gate must report no finding naming the manifest topic."""
    findings = unresolved_subscriptions([_contract()])
    offending = [f for f in findings if f.topic == MANIFEST_TOPIC]
    assert offending == [], (
        "subscriber-dispatcher-resolution still reports "
        f"{[(f.topic, f.reason) for f in offending]} for node_redeploy_orchestrator"
    )


def test_manifest_event_has_no_branch_in_the_real_handler() -> None:
    """Inverse-failure guard: the drop removes nothing the orchestrator could handle.

    Runs the REAL handler against a manifest-shaped envelope. It falls through to the
    redeploy-START default branch and fails to coerce, so ``topic_match`` would have
    replaced a DLQ drop with a handler crash rather than delivering the event.
    """
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload={
            "runtime_profile": "main",
            "contracts": [],
            "handlers": [],
            "started_at": "2026-08-31T08:08:40Z",
        },
        correlation_id=uuid4(),
        event_type="omnibase-infra.runtime-manifest-published",
    )
    # The failure is the redeploy-START coercion rejecting a manifest payload, which is
    # exactly the evidence that no legitimate branch exists for this event.
    with pytest.raises(ValidationError, match="ModelRedeployStartCommand"):
        asyncio.run(HandlerRedeployOrchestrator().handle(envelope))


def test_guard_actually_guards_readding_the_topic_is_still_unresolvable() -> None:
    """Re-declare the topic and the shipped gate must still call it a category_mismatch.

    Without this, the two assertions above would also pass on a contract whose category
    derivation had been silently fixed — they would stop discriminating against the live
    defect.
    """
    contract = _contract()
    assert contract.event_bus is not None
    with_topic = contract.model_copy(
        update={
            "event_bus": contract.event_bus.model_copy(
                update={
                    "subscribe_topics": (
                        *contract.event_bus.subscribe_topics,
                        MANIFEST_TOPIC,
                    )
                }
            )
        }
    )
    findings = [
        f for f in unresolved_subscriptions([with_topic]) if f.topic == MANIFEST_TOPIC
    ]
    assert len(findings) == 1, findings
    assert findings[0].reason == REASON_CATEGORY_MISMATCH, findings[0]
