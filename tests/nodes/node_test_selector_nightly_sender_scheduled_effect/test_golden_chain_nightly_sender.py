# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the nightly sender on the runtime schedule, and its real adapters on fake transports."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel, JsonValue

from omnimarket.handlers.schedule_run_store import InMemoryScheduleRunStore
from omnimarket.models.scm import ModelScmBusReceipt, ModelScmTerminal, ModelScmTopics
from omnimarket.nodes.node_schedule_run_reducer.handlers.handler_schedule_run_reducer import (
    HandlerScheduleRunReducer,
)
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.handlers.adapter_bus_command_sender import (
    BusNightlyCommandSender,
)
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.handlers.handler_nightly_sender_scheduled import (
    HandlerNightlySenderScheduled,
)
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.models.model_nightly_sender import (
    EnumCollectStatus,
    EnumPublishStatus,
    ModelCollectCommand,
    ModelPublishCommand,
)
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.protocols.protocol_nightly_sender import (
    NightlyCommandError,
)
from tests.nodes.node_test_selector_nightly_sender_scheduled_effect.support import (
    AFTER_NIGHTLY,
    NODE,
    REPO,
    SLOT,
    FakeRuns,
    FakeSender,
    handler,
    run_view,
    tick,
)

pytestmark = pytest.mark.unit


def terminal(topic: str, payload: dict[str, JsonValue]) -> ModelScmTerminal:
    import uuid

    return ModelScmTerminal(
        receipt=ModelScmBusReceipt(
            correlation_id=uuid.uuid4(),
            command_topic="onex.cmd.example.v1",
            command_envelope_id=uuid.uuid4(),
            terminal_topic=topic,
            terminal_envelope_id=None,
            terminal_partition=None,
            terminal_offset=None,
        ),
        failed=False,
        payload=payload,
    )


class RecordingCall:
    def __init__(self, answers: list[ModelScmTerminal | Exception]) -> None:
        self.answers = answers
        self.calls: list[tuple[ModelScmTopics, BaseModel]] = []

    def __call__(self, topics: ModelScmTopics, request: BaseModel) -> ModelScmTerminal:
        self.calls.append((topics, request))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_golden_a_finished_night_sends_both_commands_and_the_event_folds_into_the_reducer(
    tmp_path: Path,
) -> None:
    event = handler(tmp_path, FakeRuns((run_view(),)), FakeSender()).handle(
        tick(AFTER_NIGHTLY)
    )
    assert event is not None
    assert event.outcome == "completed"
    store = InMemoryScheduleRunStore()
    assert HandlerScheduleRunReducer(store).handle(event).applied is True
    held = store.get("test_selector_nightly_sender")
    assert held is not None
    assert held.last_outcome == "completed"
    producer = yaml.safe_load(
        (REPO / f"src/omnimarket/nodes/{NODE}/contract.yaml").read_text("utf-8")
    )
    reducer = yaml.safe_load(
        (
            REPO / "src/omnimarket/nodes/node_schedule_run_reducer/contract.yaml"
        ).read_text("utf-8")
    )
    assert producer["terminal_event"] == reducer["input_subscriptions"][0]["topic"]


def test_golden_the_gate_looks_once_per_interval(tmp_path: Path) -> None:
    runs = FakeRuns((run_view(status="in_progress"),))
    h = handler(tmp_path, runs, FakeSender())
    assert h.handle(tick(AFTER_NIGHTLY)) is not None
    assert h.handle(tick(AFTER_NIGHTLY + timedelta(seconds=899))) is None
    assert h.handle(tick(AFTER_NIGHTLY + timedelta(seconds=900))) is not None
    assert runs.reads == 2


def test_error_no_overlay_refuses_once_and_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = HandlerNightlySenderScheduled(environ={})
    with caplog.at_level(logging.ERROR):
        assert h.handle(tick(AFTER_NIGHTLY)) is None
        assert h.handle(tick(AFTER_NIGHTLY + timedelta(seconds=1))) is None
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert NODE in errors[0].message


def test_error_a_run_from_another_night_is_not_the_slots_run(tmp_path: Path) -> None:
    other_night = run_view(
        created=AFTER_NIGHTLY - timedelta(days=1, hours=1),
        updated=AFTER_NIGHTLY - timedelta(days=1),
    )
    sender = FakeSender()
    event = handler(tmp_path, FakeRuns((other_night,)), sender).handle(
        tick(AFTER_NIGHTLY)
    )
    assert event is not None
    assert "no nightly run for the slot yet" in event.detail
    assert sender.sent == []


def test_control_the_newest_run_of_the_night_is_the_one_that_counts(
    tmp_path: Path,
) -> None:
    older = run_view(
        run_id=1,
        status="in_progress",
        created=AFTER_NIGHTLY - timedelta(hours=1, minutes=30),
    )
    newer = run_view(run_id=2, status="completed")
    sender = FakeSender()
    event = handler(tmp_path, FakeRuns((older, newer)), sender).handle(
        tick(AFTER_NIGHTLY)
    )
    assert event is not None
    assert event.outcome == "completed"
    assert len(sender.sent) == 2


def test_bus_sender_publishes_the_command_models_on_the_contract_topics_and_reads_the_answers() -> (
    None
):
    call = RecordingCall(
        [
            terminal(
                "onex.evt.omnimarket.test-selector-collect-completed.v1",
                {
                    "result": {
                        "status": "collected",
                        "slot_date": "2026-11-22",
                        "attribution": {"files": []},
                    }
                },
            ),
            terminal(
                "onex.evt.omnimarket.test-selector-record-published.v1",
                {
                    "status": "published",
                    "slot_date": "2026-11-22",
                    "check_run_name": "x",
                },
            ),
        ]
    )
    sender = BusNightlyCommandSender(bus_lane="fixture-lane", timeout_s=1, call=call)
    collected = sender.collect_nightly(
        ModelCollectCommand(slot_date=SLOT, now=AFTER_NIGHTLY)
    )
    assert collected.status is EnumCollectStatus.COLLECTED
    # the whole collect result, attribution included, is what the publish command forwards
    assert collected.raw["attribution"] == {"files": []}
    published = sender.publish_attribution_record(
        ModelPublishCommand(
            slot_date=SLOT,
            now=AFTER_NIGHTLY,
            collect_result=collected.raw,
            selector_version="1.0.0",
            policy_version="abc123def456",
            stage_entry_counted=0,
        )
    )
    assert published.status is EnumPublishStatus.PUBLISHED
    assert [t.command for t, _ in call.calls] == [
        "onex.cmd.omnimarket.test-selector-collect-nightly-requested.v1",
        "onex.cmd.omnimarket.test-selector-publish-record-requested.v1",
    ]
    assert call.calls[0][1].model_dump(mode="json") == {
        "slot_date": "2026-11-22",
        "now": "2026-11-22T04:00:00Z",
    }


def test_error_bus_sender_raises_for_an_answer_that_is_not_a_collect_result() -> None:
    call = RecordingCall(
        [
            terminal(
                "onex.evt.omnimarket.test-selector-collect-completed.v1",
                {"status": "nonsense"},
            )
        ]
    )
    sender = BusNightlyCommandSender(bus_lane="fixture-lane", timeout_s=1, call=call)
    with pytest.raises(NightlyCommandError, match="not a collect result"):
        sender.collect_nightly(ModelCollectCommand(slot_date=SLOT, now=AFTER_NIGHTLY))


def test_error_bus_sender_passes_a_missing_terminal_event_up_as_a_command_error() -> (
    None
):
    call = RecordingCall([NightlyCommandError("no terminal event on the lane")])
    sender = BusNightlyCommandSender(bus_lane="fixture-lane", timeout_s=1, call=call)
    with pytest.raises(NightlyCommandError):
        sender.collect_nightly(ModelCollectCommand(slot_date=SLOT, now=AFTER_NIGHTLY))
