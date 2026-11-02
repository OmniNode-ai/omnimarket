# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The orchestrator replies to the per-principal topics the request names (OMN-22103).

The command carries three reply topic names, each of the shape its contract row
declares, all for one principal. The orchestrator publishes prepared, fallback
and the package reply to the topics the request names and `activated` to its
static contract topic.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from omnibase_core.enums.enum_client_verdict_action import EnumClientVerdictAction
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import ValidationError

from omnimarket.models.context_compaction import (
    EnumContextCompactionTrigger,
    ModelContextCompactionActivated,
    ModelContextCompactionFallback,
    ModelContextCompactionPackageReply,
    ModelContextCompactionPrepared,
    ModelContextCompactionRequested,
    load_reply_topic_templates,
)
from omnimarket.nodes.node_context_compaction_orchestrator.container.container_context_compaction_orchestrator import (
    create_handler_context_compaction_orchestrator,
)
from omnimarket.nodes.node_context_compaction_orchestrator.event_topics import (
    publish_topic_for,
)

from .test_handler_context_compaction_orchestrator import ACTIVATION, INVOCATION, _bus

pytestmark = pytest.mark.unit

NODE_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_context_compaction_orchestrator/contract.yaml"
)
PRINCIPAL = "dev-test-principal"
OTHER = "other-principal"
PACKAGE = f"onex.cmd.omniintelligence.context-compaction-package-for-{PRINCIPAL}.v1"
PREPARED = f"onex.evt.omnimarket.context-compaction-prepared-for-{PRINCIPAL}.v1"
FALLBACK = f"onex.evt.omnimarket.context-compaction-fallback-for-{PRINCIPAL}.v1"
ACTIVATED = "onex.evt.omnimarket.context-compaction-activated.v1"
CONTRACT_PRINCIPAL_PATTERN = "^[a-z0-9][a-z0-9-]{0,62}$"


def _fields(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "invocation_id": INVOCATION,
        "session_id": "session-1",
        "cwd": "/work/repo",
        "trigger": EnumContextCompactionTrigger.MANUAL,
        "input_messages_hash": hashlib.sha256(b"input").hexdigest(),
        "newest_turn": "turn-9",
        "barrier_id": "barrier-1",
        "reply_topic_package": PACKAGE,
        "reply_topic_prepared": PREPARED,
        "reply_topic_fallback": FALLBACK,
    }
    fields.update(overrides)
    return fields


def test_compaction_reply_topic_contract_declares_the_three_templates_in_parts() -> (
    None
):
    contract = yaml.safe_load(NODE_CONTRACT.read_text(encoding="utf-8"))
    rows = {
        r["event_type"]: r for r in contract["published_events"] if "topic_parts" in r
    }
    assert set(rows) == {
        "ContextCompactionPrepared",
        "ContextCompactionFallback",
        "ContextCompactionPackageReply",
    }
    for row in rows.values():
        assert "topic" not in row
        assert row["topic_parts"]["principal_pattern"] == CONTRACT_PRINCIPAL_PATTERN
    templates = load_reply_topic_templates()
    assert templates.package.topic_for(PRINCIPAL) == PACKAGE
    assert templates.prepared.topic_for(PRINCIPAL) == PREPARED
    assert templates.fallback.topic_for(PRINCIPAL) == FALLBACK
    static = [r for r in contract["published_events"] if "topic" in r]
    assert [r["topic"] for r in static] == [ACTIVATED]
    assert "inputs" in contract
    for name in ("reply_topic_package", "reply_topic_prepared", "reply_topic_fallback"):
        assert contract["inputs"][name]["required"] is True


def test_compaction_reply_topic_model_accepts_three_topics_of_one_principal() -> None:
    command = ModelContextCompactionRequested(**_fields())
    assert command.reply_topic_package == PACKAGE
    assert command.reply_topic_prepared == PREPARED
    assert command.reply_topic_fallback == FALLBACK


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        (
            "reply_topic_package",
            "onex.cmd.omniintelligence.context-compaction-package.v1",
        ),
        ("reply_topic_prepared", "onex.evt.omnimarket.context-compaction-prepared.v1"),
        ("reply_topic_fallback", "onex.evt.omnimarket.anything-else-for-dev.v1"),
        # A topic of another family, even with a valid principal slot.
        ("reply_topic_prepared", FALLBACK),
        ("reply_topic_package", PACKAGE + ".extra"),
        ("reply_topic_package", "x" + PACKAGE),
        ("reply_topic_fallback", FALLBACK.replace(PRINCIPAL, "Bad_Principal")),
        ("reply_topic_fallback", FALLBACK.replace(PRINCIPAL, "{principal}")),
        ("reply_topic_package", ""),
    ],
)
def test_compaction_reply_topic_model_refuses_a_topic_not_of_the_declared_pattern(
    field: str, bad: str
) -> None:
    with pytest.raises(ValidationError, match=field):
        ModelContextCompactionRequested(**_fields(**{field: bad}))


def test_compaction_reply_topic_model_refuses_mismatched_principals() -> None:
    with pytest.raises(ValidationError, match="principal"):
        ModelContextCompactionRequested(
            **_fields(reply_topic_prepared=PREPARED.replace(PRINCIPAL, OTHER))
        )


@pytest.mark.parametrize(
    "field", ["reply_topic_package", "reply_topic_prepared", "reply_topic_fallback"]
)
def test_compaction_reply_topic_model_requires_each_field(field: str) -> None:
    fields = _fields()
    del fields[field]
    with pytest.raises(ValidationError, match=field):
        ModelContextCompactionRequested(**fields)


def test_compaction_reply_topic_all_three_for_another_principal_is_accepted() -> None:
    command = ModelContextCompactionRequested(
        **_fields(
            reply_topic_package=PACKAGE.replace(PRINCIPAL, OTHER),
            reply_topic_prepared=PREPARED.replace(PRINCIPAL, OTHER),
            reply_topic_fallback=FALLBACK.replace(PRINCIPAL, OTHER),
        )
    )
    assert OTHER in command.reply_topic_package


def _request() -> ModelContextCompactionRequested:
    return ModelContextCompactionRequested(**_fields())


def _envelope_topics(events: Any) -> list[tuple[str, type]]:
    return [
        (e.event_type, type(e.payload))
        if isinstance(e, ModelEventEnvelope)
        else (publish_topic_for(e), type(e))
        for e in events
    ]


def test_compaction_reply_topic_native_fallback_replies_to_the_requested_topics() -> (
    None
):
    output = asyncio.run(
        create_handler_context_compaction_orchestrator().handle_async(_request())
    )
    assert output.correlation_id == INVOCATION
    assert _envelope_topics(output.events) == [
        (FALLBACK, ModelContextCompactionFallback),
        (PACKAGE, ModelContextCompactionPackageReply),
    ]
    for event in output.events:
        assert isinstance(event, ModelEventEnvelope)
        assert event.correlation_id == INVOCATION
    reply = output.events[-1].payload
    assert reply.action is EnumClientVerdictAction.NATIVE


def test_compaction_reply_topic_install_replies_to_the_requested_topics(
    compaction_bus: Any,
) -> None:
    _, handler = _bus(compaction_bus, activation_ids=lambda: ACTIVATION)
    output = asyncio.run(handler.handle_async(_request()))
    assert _envelope_topics(output.events) == [
        (PREPARED, ModelContextCompactionPrepared),
        (ACTIVATED, ModelContextCompactionActivated),
        (PACKAGE, ModelContextCompactionPackageReply),
    ]
    activated = output.events[1]
    # activated stays on its static contract topic and is routed by class name.
    assert not isinstance(activated, ModelEventEnvelope)


def test_compaction_reply_topic_another_principal_gets_its_own_topics() -> None:
    request = ModelContextCompactionRequested(
        **_fields(
            reply_topic_package=PACKAGE.replace(PRINCIPAL, OTHER),
            reply_topic_prepared=PREPARED.replace(PRINCIPAL, OTHER),
            reply_topic_fallback=FALLBACK.replace(PRINCIPAL, OTHER),
        )
    )
    output = asyncio.run(
        create_handler_context_compaction_orchestrator().handle_async(request)
    )
    topics = [t for t, _ in _envelope_topics(output.events)]
    assert topics == [
        FALLBACK.replace(PRINCIPAL, OTHER),
        PACKAGE.replace(PRINCIPAL, OTHER),
    ]
    assert all(PRINCIPAL not in t for t in topics)


def test_compaction_reply_topic_publish_topic_for_refuses_an_undeclared_topic() -> None:
    bad = ModelEventEnvelope(
        payload=ModelContextCompactionFallback(
            invocation_id=UUID(int=1),
            session_id="s",
            failure_class=next(iter(_failure_classes())),
        ),
        event_type=PREPARED.replace("prepared", "not-declared"),
        correlation_id=UUID(int=1),
    )
    with pytest.raises(ValueError, match="not-declared"):
        publish_topic_for(bad)


def _failure_classes() -> Any:
    from omnimarket.models.context_compaction import EnumContextCompactionFailureClass

    return EnumContextCompactionFailureClass
