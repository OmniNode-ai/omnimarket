# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22478: the install-confirm trigger gets its reader from the contract, or skips.

After the projected-key fix the runtime validated every
``projection-session-content-applied`` event and called the trigger handler with the
event alone. ``handle()`` declared ``reader`` as a REQUIRED keyword-only argument and
``ProtocolSessionCompactSource`` has no implementation, so each call raised
``HandlerContextInstallConfirmTrigger.handle() missing 1 required keyword-only
argument: 'reader'`` and the record went back to the dead-letter topic
(about 232 records a minute on dev, re-delivered up to five times).

``RECORDED_PAYLOAD`` is the ``payload`` of one real dead-letter record
(``original_message.value`` at offset 23357700 of ``onex.dlq.omnibase-infra.events.v1``,
2026-11-06T09:48:58Z), the same record the sibling test replays through validation.
The tests call the handler the way the runtime does: built with no arguments, then
``handle(event)``.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
import yaml

from omnimarket.models.context_install_confirm_trigger import (
    EnumInstallConfirmTriggerOutcome,
    ModelInstallConfirmSkipped,
    ModelInstallConfirmTriggerResult,
    ModelSessionCompactSnapshot,
    ModelSessionContentAppliedEvent,
)
from omnimarket.nodes.node_context_install_confirm_trigger_effect.handlers.handler_context_install_confirm_trigger import (
    HandlerContextInstallConfirmTrigger,
)
from tests.nodes.node_context_install_confirm_request_compute.test_handler_context_install_confirm_request import (
    SESSION,
    activation,
    chunks,
    package,
)

pytestmark = pytest.mark.unit

RECORDED_PAYLOAD = (
    '{"rows_upserted":1,"event_id":'
    '"1a1feaba4b4ad77234eae5cc83c8d5336c86f6835f3cd8b40f861a0738a4b42f",'
    '"projected":true}'
)
CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_context_install_confirm_trigger_effect/contract.yaml"
)


class _Reader:
    def __init__(self, snapshot: ModelSessionCompactSnapshot | None) -> None:
        self.snapshot = snapshot
        self.read_ids: list[str] = []

    def read(self, event_id: str) -> ModelSessionCompactSnapshot | None:
        self.read_ids.append(event_id)
        return self.snapshot


def _recorded_event() -> ModelSessionContentAppliedEvent:
    return ModelSessionContentAppliedEvent.model_validate(json.loads(RECORDED_PAYLOAD))


def _full_snapshot() -> ModelSessionCompactSnapshot:
    return ModelSessionCompactSnapshot(
        session_id=SESSION, chunks=chunks(package(), 3), activations=(activation(),)
    )


def test_golden_recorded_record_with_no_reader_is_a_typed_skip_not_a_crash() -> None:
    # Exactly the runtime's call: zero-argument construction, then handle(event).
    event = _recorded_event()

    result = HandlerContextInstallConfirmTrigger().handle(event)

    assert result == ModelInstallConfirmSkipped(event_id=event.event_id)
    assert result.reason == "source_unavailable"
    assert result.dependency == "ProtocolSessionCompactSource"


def test_golden_constructor_injected_reader_drives_the_confirmation() -> None:
    reader = _Reader(_full_snapshot())
    event = _recorded_event()

    result = HandlerContextInstallConfirmTrigger(reader=reader).handle(event)

    assert reader.read_ids == [event.event_id]
    assert isinstance(result, ModelInstallConfirmTriggerResult)
    assert result.outcome is EnumInstallConfirmTriggerOutcome.REQUEST_READY
    assert result.confirmation is not None


def test_golden_keyword_reader_still_wins_over_the_injected_one() -> None:
    injected = _Reader(None)
    explicit = _Reader(_full_snapshot())

    result = HandlerContextInstallConfirmTrigger(reader=injected).handle(
        _recorded_event(), reader=explicit
    )

    assert isinstance(result, ModelInstallConfirmTriggerResult)
    assert result.outcome is EnumInstallConfirmTriggerOutcome.REQUEST_READY
    assert injected.read_ids == []
    assert len(explicit.read_ids) == 1


def test_error_injected_reader_that_knows_no_session_is_session_unresolved() -> None:
    result = HandlerContextInstallConfirmTrigger(reader=_Reader(None)).handle(
        _recorded_event()
    )

    assert isinstance(result, ModelInstallConfirmTriggerResult)
    assert result.outcome is EnumInstallConfirmTriggerOutcome.SESSION_UNRESOLVED


def test_error_a_failing_reader_raises_and_is_never_turned_into_a_skip() -> None:
    class _Broken:
        def read(self, event_id: str) -> ModelSessionCompactSnapshot | None:
            raise ConnectionError(event_id)

    with pytest.raises(ConnectionError):
        HandlerContextInstallConfirmTrigger(reader=_Broken()).handle(_recorded_event())


def test_error_nothing_applied_is_not_applied_even_without_a_reader() -> None:
    event = ModelSessionContentAppliedEvent(event_id="e-1", rows_upserted=0)

    result = HandlerContextInstallConfirmTrigger().handle(event)

    assert isinstance(result, ModelInstallConfirmTriggerResult)
    assert result.outcome is EnumInstallConfirmTriggerOutcome.NOT_APPLIED


def test_error_the_skip_repeats_the_same_way_on_every_redelivery() -> None:
    handler = HandlerContextInstallConfirmTrigger()
    event = _recorded_event()

    assert [handler.handle(event) for _ in range(5)] == [handler.handle(event)] * 5


def test_the_runtime_can_build_the_handler_with_no_constructor_arguments() -> None:
    # The core handler resolver builds a handler with no required constructor
    # parameter through its zero-argument path; a required one is a hard failure.
    required = [
        name
        for name, parameter in inspect.signature(
            HandlerContextInstallConfirmTrigger
        ).parameters.items()
        if parameter.default is inspect.Parameter.empty
    ]
    assert required == []
    handle_required = [
        name
        for name, parameter in inspect.signature(
            HandlerContextInstallConfirmTrigger.handle
        ).parameters.items()
        if parameter.default is inspect.Parameter.empty and name != "self"
    ]
    assert handle_required == ["applied_event"]


def test_the_contract_declares_the_reader_as_an_optional_injected_dependency() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())

    declared = [dep for dep in contract["dependencies"] if dep["name"] == "reader"]

    assert len(declared) == 1
    assert declared[0]["dependency_type"] == "protocol"
    assert declared[0]["class_name"] == "ProtocolSessionCompactSource"
    assert (
        declared[0]["module"] == "omnimarket.protocols.protocol_session_compact_source"
    )
    assert declared[0]["required"] is False


def test_the_contract_routes_the_skip_event_to_its_own_topic_only() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    skipped = "onex.evt.omnimarket.context-install-confirm-skipped.v1"

    assert contract["published_events"] == [
        {
            "event_type": "ModelInstallConfirmSkipped",
            "topic": skipped,
            "description": "No session compact source was injected, so nothing was read.",
        }
    ]
    assert skipped in contract["event_bus"]["publish_topics"]
    assert skipped != contract["terminal_event"]
