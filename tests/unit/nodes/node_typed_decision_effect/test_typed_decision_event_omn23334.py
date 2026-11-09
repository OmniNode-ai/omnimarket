# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A typed decision leaves one delegation_events row (OMN-23334).

The golden chain runs the real decision handler over a fake transport, hands the
terminal it published to the real delegation projection, and reads the row back
from the in-memory database. The error chains do the same for a backend error
and a refusal. The control: take the write out of the handler and the golden
test fails on an empty publisher.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import httpx
import pytest
from omnibase_spi.protocols.services import ProtocolSecretStore

from omnimarket.events.emit_effect_topic_publisher import EmitEffectTopicPublisher
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.nodes.node_typed_decision_effect.container.container_typed_decision_effect import (
    create_handler_typed_decision,
)
from omnimarket.nodes.node_typed_decision_effect.handlers.handler_typed_decision import (
    HandlerTypedDecision,
)
from omnimarket.nodes.node_typed_decision_effect.handlers.typed_decision_event import (
    InMemoryTypedDecisionEventPublisher,
    ModelTypedDecisionEvent,
)
from omnimarket.nodes.node_typed_decision_effect.models.model_typed_decision import (
    EnumTypedDecisionDecider,
)
from omnimarket.projection.protocol_database import (
    DatabaseAdapter,
    InmemoryDatabaseAdapter,
)
from tests.unit.nodes.node_typed_decision_effect.test_handler_typed_decision import (
    _BACKEND,
    _BACKEND_ID,
    _BIFROST,
    _FAKE_KEY,
    _REFUSED,
    _REFUSED_NAME,
    _choice,
    _choice_answer,
    _digest,
    _Recorder,
    _Store,
)

pytestmark = pytest.mark.unit


def _handler(
    recorder: _Recorder, publisher: InMemoryTypedDecisionEventPublisher, tmp: Path
) -> HandlerTypedDecision:
    return HandlerTypedDecision(
        transport=httpx.MockTransport(recorder),
        secret_store=cast(
            ProtocolSecretStore, _Store({_BACKEND["secret_ref"]: _FAKE_KEY})
        ),
        bifrost_config_path=_BIFROST,
        bifrost_overlay_path=tmp / "no-overlay.yaml",
        refused_repositories=(_digest(_REFUSED_NAME),),
        event_publisher=publisher,
    )


def _project(event: ModelTypedDecisionEvent) -> list[dict[str, object]]:
    """Fold one published terminal through the real projection; return its rows."""
    db: DatabaseAdapter = InmemoryDatabaseAdapter()
    # The mirror is left empty, as on the lab evidence target: the declared tenant
    # must resolve without a registry row.
    HandlerProjectionDelegation().handle(
        {**event.payload, "_event_type": event.topic, "_db": db}
    )
    return db.query("delegation_events", {"correlation_id": event.correlation_id})


def test_typed_decision_event_golden_model_decision_is_one_completed_row(
    tmp_path: Path,
) -> None:
    publisher = InMemoryTypedDecisionEventPublisher()
    request = _choice()
    result = _handler(
        _Recorder(_choice_answer("change", 0.93)), publisher, tmp_path
    ).handle(request)
    assert result.decided_by is EnumTypedDecisionDecider.MODEL

    (event,) = publisher.events
    assert event.topic.endswith("delegate-skill-completed.v1")
    assert event.correlation_id == str(request.correlation_id)
    assert event.payload["status"] == "completed"
    assert event.payload["backend_id"] == _BACKEND_ID
    assert event.payload["provider"] == "typesafe"
    assert event.payload["task_type"] == "typed_decision"
    assert event.payload["quality_gate_passed"] is True
    assert event.payload["model_name"] == "jev-9.9.9"
    # The question and the work-unit state never ride in the event.
    assert "prompt_text" not in event.payload
    assert "ruff format" not in str(event.payload)
    assert _FAKE_KEY not in str(event.payload)

    (row,) = _project(event)
    assert row["correlation_id"] == str(request.correlation_id)
    assert row["backend_id"] == _BACKEND_ID
    assert row["task_type"] == "typed_decision"
    assert row["model_name"] == "jev-9.9.9"
    assert row["terminal_ok"] is True
    assert row["quality_gate_passed"] is True
    assert row["tokens_input"] == 300
    assert row["tokens_output"] == 20


def test_typed_decision_event_abstention_is_a_completed_row_the_model_did_not_decide(
    tmp_path: Path,
) -> None:
    publisher = InMemoryTypedDecisionEventPublisher()
    _handler(_Recorder(_choice_answer("change", 0.55)), publisher, tmp_path).handle(
        _choice()
    )
    (event,) = publisher.events
    assert event.payload["status"] == "completed"
    assert event.payload["quality_gate_passed"] is False
    (row,) = _project(event)
    assert row["backend_id"] == _BACKEND_ID
    assert row["quality_gate_passed"] is False


def test_typed_decision_event_error_backend_http_error_is_one_failed_row(
    tmp_path: Path,
) -> None:
    publisher = InMemoryTypedDecisionEventPublisher()
    request = _choice()
    result = _handler(
        _Recorder(httpx.Response(503, text="upstream down")), publisher, tmp_path
    ).handle(request)
    assert result.decided_by is EnumTypedDecisionDecider.INCUMBENT_BACKEND_ERROR

    (event,) = publisher.events
    assert event.topic.endswith("delegate-skill-failed.v1")
    assert event.payload["status"] == "failed"
    assert event.payload["backend_id"] == _BACKEND_ID
    assert "backend_http_error" in str(event.payload["error_message"])
    (row,) = _project(event)
    assert row["correlation_id"] == str(request.correlation_id)
    assert row["backend_id"] == _BACKEND_ID
    assert row["terminal_ok"] is False


def test_typed_decision_event_error_refused_repository_is_one_failed_row_with_nothing_sent(
    tmp_path: Path,
) -> None:
    publisher = InMemoryTypedDecisionEventPublisher()
    recorder = _Recorder(_choice_answer("change", 0.99))
    _handler(recorder, publisher, tmp_path).handle(_choice(_REFUSED))
    assert recorder.decision_calls == []
    (event,) = publisher.events
    assert event.topic.endswith("delegate-skill-failed.v1")
    assert "repository_refused" in str(event.payload["error_message"])
    (row,) = _project(event)
    assert row["terminal_ok"] is False


def test_typed_decision_event_publisher_failure_never_fails_the_decision(
    tmp_path: Path,
) -> None:
    class _Raising:
        def publish(self, **_: object) -> bool:
            raise RuntimeError("broker down")

    handler = _handler(
        _Recorder(_choice_answer("change", 0.93)),
        InMemoryTypedDecisionEventPublisher(),
        tmp_path,
    )
    handler._event_publisher = _Raising()
    result = handler.handle(_choice())
    assert result.decided_by is EnumTypedDecisionDecider.MODEL


def test_typed_decision_event_the_container_binds_the_emit_effect_publisher() -> None:
    handler = create_handler_typed_decision()
    assert isinstance(handler._event_publisher, EmitEffectTopicPublisher)
