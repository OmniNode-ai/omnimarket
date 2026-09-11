# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_model_router's declared outputs are the ones its code actually names.

OMN-18159. Adopting omnibase-core 0.47.9 touched
``handlers/handler_model_router.py`` -- ``routing_decision_id`` became a
``UUID`` and ``served_model_id`` a ``ModelServedModelRef``. The
contract-state-coverage gate is changed-only, so touching that file brought
this node into scope for the first time and found FIVE declared outputs with
no test naming them: the two ``outputs`` event keys and the four result and
route topics.

That gap was latent, not new, and the gate is right that it matters: the
contract is what the runtime dispatches on, so a topic renamed in the contract
and not in the module -- or the reverse -- is a node that publishes where
nothing listens. Every assertion below compares a literal against a value read
from a real artifact (the committed contract, or the module constant the
handler publishes with), so it fails on either side drifting rather than
restating itself.

The two ``model-router-{completed,failed}`` topics are emitted by the RUNTIME
from ``runtime_dispatch.terminal_events``, not by the handler, which is why
they are asserted through the contract block that declares them rather than
through a publish.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_model_router.handlers.handler_model_router import (
    TOPIC_MODEL_LLM_ROUTE_REJECTED,
    TOPIC_MODEL_LLM_ROUTE_RESOLVED,
    HandlerModelRouter,
)

_CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_model_router"
    / "contract.yaml"
)


def _contract() -> dict[str, Any]:
    document = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{_CONTRACT} did not parse to a mapping"
    return document


def test_the_route_topics_the_handler_publishes_are_the_declared_ones() -> None:
    """The module constants and the contract name the same two topics."""
    publish_topics = _contract()["event_bus"]["publish_topics"]

    assert TOPIC_MODEL_LLM_ROUTE_RESOLVED == (
        "onex.evt.omnimarket.model-llm-route-resolved.v1"
    )
    assert TOPIC_MODEL_LLM_ROUTE_REJECTED == (
        "onex.evt.omnimarket.model-llm-route-rejected.v1"
    )
    assert TOPIC_MODEL_LLM_ROUTE_RESOLVED in publish_topics
    assert TOPIC_MODEL_LLM_ROUTE_REJECTED in publish_topics


def test_the_terminal_topics_the_runtime_dispatches_are_declared_and_published() -> (
    None
):
    """A terminal topic absent from publish_topics is one nothing may emit."""
    document = _contract()
    terminal = document["runtime_dispatch"]["terminal_events"]
    publish_topics = document["event_bus"]["publish_topics"]

    assert terminal["success"] == "onex.evt.omnimarket.model-router-completed.v1"
    assert terminal["failure"] == "onex.evt.omnimarket.model-router-failed.v1"
    assert terminal["success"] in publish_topics
    assert terminal["failure"] in publish_topics


def test_both_route_event_outputs_are_declared_with_their_core_models() -> None:
    """The two event outputs, and the core models they are typed against.

    ``route_resolved_event`` and ``route_rejected_event`` are what the handler
    builds; core 0.47.9 changed both models' field types, so a declaration
    pointing at the wrong module is how that change would go unnoticed.
    """
    outputs = _contract()["outputs"]

    assert outputs["route_resolved_event"]["type"] == "ModelLlmRouteResolvedEvent"
    assert outputs["route_resolved_event"]["module"] == (
        "omnibase_core.models.routing.model_llm_route_resolved_event"
    )
    assert outputs["route_rejected_event"]["type"] == "ModelLlmRouteRejectedEvent"
    assert outputs["route_rejected_event"]["module"] == (
        "omnibase_core.models.routing.model_llm_route_rejected_event"
    )


def test_a_registry_entry_with_no_provider_records_an_absence_not_a_guess() -> None:
    """``ModelServedModelRef`` refuses an empty provider; ``{}`` is reachable.

    The rejected path resolves its registry entry with ``.get(model_key, {})``,
    so a model key that routed to nothing arrives here as an empty dict. The
    ref must still be constructible, and what it records has to read as an
    absence rather than as a provider somebody could act on.
    """
    ref = HandlerModelRouter._served_model_id("qwen3-coder-30b", {})

    assert ref.provider == "unrecorded"
    assert str(ref.model_id.root) == "qwen3-coder-30b"


def test_a_registry_entry_with_a_provider_records_that_provider() -> None:
    """The absence literal never displaces a real value."""
    ref = HandlerModelRouter._served_model_id(
        "qwen3-coder-30b",
        {"provider": "local", "served_model_id": "qwen/qwen3-coder-30b"},
    )

    assert ref.provider == "local"
    assert str(ref.model_id.root) == "qwen/qwen3-coder-30b"
