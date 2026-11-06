# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22478: an applied event the runtime cannot read a session for is skipped, not dead-lettered.

After the projected-key fix (OMN-22623) every ``projection-session-content-applied`` event
reached the trigger handler, and the handler failed on all of them: ``handle()`` declared a
keyword-only ``reader`` that the runtime's dispatcher never passes, and
``ProtocolSessionCompactSource`` has no implementation yet (OMN-21115, the compaction line).
Each record went to ``onex.dlq.omnibase-infra.events.v1`` at about 232 a minute, and the
replay node re-delivered each up to five times.

``RECORDED_ENVELOPE`` is ``original_message.value`` of one such dead-letter record, byte for
byte (source topic partition 0, offset 3174643, 2026-11-06T13:46:12Z). That record's
``failure_reason`` ends ``TypeError: HandlerContextInstallConfirmTrigger.handle() missing 1
required keyword-only argument: 'reader'``. The golden test boots the real contract through
``wire_from_manifest`` on an in-memory bus, publishes the recorded bytes, and reads one typed
skip event on the contract's skipped topic and nothing on the completed topic. Run against
the handler before this change the same test sees no skip event: the dispatcher fails with
that TypeError and the event goes to the dead-letter topic.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from pydantic import ValidationError

from omnimarket.models.context_install_confirm_trigger import (
    EnumInstallConfirmSkipReason,
    EnumInstallConfirmTriggerOutcome,
    ModelInstallConfirmSkipped,
    ModelInstallConfirmTriggerResult,
    ModelSessionCompactSnapshot,
    ModelSessionContentAppliedEvent,
)
from omnimarket.nodes.node_context_install_confirm_trigger_effect.handlers.handler_context_install_confirm_trigger import (
    HandlerContextInstallConfirmTrigger,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_context_install_confirm_trigger_effect"
)
CONTRACT = NODE_DIR / "contract.yaml"
APPLIED_TOPIC = "onex.evt.omnimarket.projection-session-content-applied.v1"
SKIPPED_TOPIC = "onex.evt.omnimarket.context-install-confirm-skipped.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.context-install-confirm-completed.v1"

RECORDED_EVENT_ID = "9e2083991eb5adb57db19080b80d100ad8e455b1360c13342399b3d50f6047a9"
RECORDED_ENVELOPE = (
    '{"payload":{"rows_upserted":1,"event_id":"' + RECORDED_EVENT_ID + '",'
    '"projected":true},"envelope_id":"753e2fdc-be68-4676-80cd-3ed9f8e7e4a1",'
    '"envelope_timestamp":"2026-11-06T13:46:12.351875Z",'
    '"correlation_id":"51a95d4a-2f42-42bc-93e7-8ac625fbac1a","parent_envelope_id":null,'
    '"source_tool":"projection-reducer","target_tool":null,"metadata":{"trace_id":null,'
    '"span_id":null,"parent_span_id":null,"request_id":null,"headers":{},"tags":{}},'
    '"security_context":null,"event_type":"' + APPLIED_TOPIC + '","payload_type":null,'
    '"payload_schema_version":null,"priority":5,"timeout_seconds":null,"retry_count":0,'
    '"request_id":null,"trace_id":null,"span_id":null,"tenant_id":null,"producer":null,'
    '"onex_version":{"major":1,"minor":0,"patch":0,"prerelease":null,"build":null},'
    '"envelope_version":{"major":2,"minor":3,"patch":0,"prerelease":null,"build":null}}'
)


def _contract() -> dict[str, Any]:
    raw = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


async def _publish_recorded_envelope(
    *, wait_seconds: float = 20
) -> dict[str, list[dict[str, Any]]]:
    """Boot the real contract on an in-memory bus, publish the recorded bytes, read the topics."""
    manifest = discover_contracts_from_paths([CONTRACT])
    assert not manifest.errors, manifest.errors
    bus = EventBusInmemory(environment="skip-boot", group="skip-boot")
    await bus.start()
    seen: dict[str, list[dict[str, Any]]] = {SKIPPED_TOPIC: [], COMPLETED_TOPIC: []}
    arrived = asyncio.Event()

    def _collector(topic: str) -> Callable[[object], Awaitable[None]]:
        async def _collect(message: object) -> None:
            seen[topic].append(json.loads(bytes(getattr(message, "value", b""))))
            arrived.set()

        return _collect

    for topic in seen:
        await bus.subscribe(
            topic, on_message=_collector(topic), group_id=f"probe-{topic}"
        )

    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
        engine,
        event_bus=bus,
        environment="local",
    )
    assert report.total_failed == 0
    assert report.total_wired == 1
    engine.freeze()

    await bus.publish(APPLIED_TOPIC, None, RECORDED_ENVELOPE.encode("utf-8"), None)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(arrived.wait(), timeout=wait_seconds)
    await asyncio.sleep(0.5)
    await bus.close()
    return seen


async def test_golden_the_recorded_dead_letter_envelope_yields_one_typed_skip_event() -> (
    None
):
    seen = await _publish_recorded_envelope()

    assert seen[COMPLETED_TOPIC] == [], (
        "a skipped event must not look like a confirmation"
    )
    assert len(seen[SKIPPED_TOPIC]) == 1, "the recorded envelope produced no skip event"
    payload = seen[SKIPPED_TOPIC][0]["payload"]
    skipped = ModelInstallConfirmSkipped.model_validate(payload)
    assert skipped.event_id == RECORDED_EVENT_ID
    assert skipped.rows_upserted == 1
    assert skipped.reason is EnumInstallConfirmSkipReason.READER_NOT_WIRED


def test_golden_the_runtime_builds_the_handler_with_no_arguments_and_it_skips() -> None:
    """The resolver constructs the handler from the contract with no reader."""
    applied = ModelSessionContentAppliedEvent.model_validate(
        json.loads(RECORDED_ENVELOPE)["payload"]
    )

    skipped = HandlerContextInstallConfirmTrigger().handle(applied)

    assert isinstance(skipped, ModelInstallConfirmSkipped)
    assert skipped.event_id == RECORDED_EVENT_ID
    assert skipped.reason is EnumInstallConfirmSkipReason.READER_NOT_WIRED


def test_golden_the_contract_declares_the_reader_optional_and_the_skip_topic() -> None:
    contract = _contract()
    (dependency,) = contract["dependencies"]
    assert dependency["name"] == "reader"
    assert dependency["required"] is False
    assert SKIPPED_TOPIC in contract["event_bus"]["publish_topics"]
    assert {
        entry["event_type"]: entry["topic"] for entry in contract["published_events"]
    } == {"InstallConfirmSkipped": SKIPPED_TOPIC}
    parameter = inspect.signature(HandlerContextInstallConfirmTrigger).parameters[
        "reader"
    ]
    assert parameter.default is None
    handle_parameters = inspect.signature(
        HandlerContextInstallConfirmTrigger.handle
    ).parameters
    assert [p for p in handle_parameters if p != "self"] == ["applied_event"]


def test_golden_a_wired_reader_still_takes_the_confirmation_path() -> None:
    class _Reader:
        def read(self, event_id: str) -> ModelSessionCompactSnapshot | None:
            return None

    applied = ModelSessionContentAppliedEvent(event_id="e", rows_upserted=1)
    out = HandlerContextInstallConfirmTrigger(reader=_Reader()).handle(applied)
    assert isinstance(out, ModelInstallConfirmTriggerResult)
    assert out.outcome is EnumInstallConfirmTriggerOutcome.SESSION_UNRESOLVED


def test_error_a_failing_reader_is_never_converted_into_a_skip() -> None:
    """A wired reader that fails raises: only an absent reader is a skip."""

    class _Broken:
        def read(self, event_id: str) -> ModelSessionCompactSnapshot | None:
            raise ConnectionError("session store unreachable")

    applied = ModelSessionContentAppliedEvent(event_id="e", rows_upserted=1)
    with pytest.raises(ConnectionError, match="session store unreachable"):
        HandlerContextInstallConfirmTrigger(reader=_Broken()).handle(applied)


def test_error_the_skip_event_is_strict_and_frozen() -> None:
    good = {
        "event_id": RECORDED_EVENT_ID,
        "rows_upserted": 1,
        "reason": "reader_not_wired",
    }
    skipped = ModelInstallConfirmSkipped.model_validate(good)
    assert skipped.model_config["frozen"] is True
    for bad in (
        good | {"unexpected": True},
        good | {"reason": "unknown_reason"},
        good | {"event_id": ""},
        good | {"rows_upserted": -1},
    ):
        with pytest.raises(ValidationError):
            ModelInstallConfirmSkipped.model_validate(bad)
