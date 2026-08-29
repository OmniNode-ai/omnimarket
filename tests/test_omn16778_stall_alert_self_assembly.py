# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node assembles its own evaluation (OMN-16778, operator-approved redesign).

RED baseline these tests break, captured live on the ``.201`` dev lane at
2026-08-29T01:3x and reproduced byte-for-byte before the change::

    RED: ValidationError: 8 validation errors for ModelConsumerFlowStallAlertRequest
       consumer_group -> missing        - Field required
       topic          -> missing        - Field required
       correlation_id -> missing        - Field required
       windows        -> missing        - Field required
       policy         -> missing        - Field required
       rows_upserted  -> extra_forbidden - Extra inputs are not permitted
       flow_rows      -> extra_forbidden - Extra inputs are not permitted
       projected      -> extra_forbidden - Extra inputs are not permitted

Every test here drives the REAL handler with an injected reader and an injected
publisher. Nothing is stubbed except the two I/O edges, which is the whole point
of putting them behind protocols.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml

from omnimarket.models.enum_consumer_flow_state import EnumConsumerFlowState
from omnimarket.nodes.node_consumer_flow_stall_alert_effect.handlers import (
    HandlerConsumerFlowStallAlert,
)
from omnimarket.nodes.node_consumer_flow_stall_alert_effect.models import (
    EnumStallAlertOutcome,
    ModelConsumerFlowStallAlertTrigger,
    ModelFlowWindowObservation,
    load_stall_alert_policy,
    load_windows_source,
)
from omnimarket.nodes.node_slack_publish_effect.models.model_slack_publish import (
    ModelSlackPublish,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE_ROOT = (
    REPO_ROOT / "src" / "omnimarket" / "nodes" / "node_consumer_flow_stall_alert_effect"
)
CONTRACT_PATH = NODE_ROOT / "contract.yaml"

_EPOCH = datetime(2026, 8, 29, 1, 0, tzinfo=UTC)
_WINDOW = timedelta(minutes=1)

#: The channel value the fake secret store hands back. A test that let the real
#: resolver run would be testing the operator's environment, not this node.
_TEST_CHANNEL = "C0TESTCHANNEL"


class _FakeWindowReader:
    """In-memory stand-in for the projection read, keyed the same way."""

    def __init__(
        self, history: dict[tuple[str, str], tuple[ModelFlowWindowObservation, ...]]
    ) -> None:
        self._history = history
        self.calls: list[tuple[str, str, int]] = []

    def read_history(
        self, *, consumer_group: str, topic: str, limit: int
    ) -> tuple[ModelFlowWindowObservation, ...]:
        self.calls.append((consumer_group, topic, limit))
        return self._history.get((consumer_group, topic), ())[-limit:]


class _CapturingPublisher:
    """Records what the handler put on the bus, without a bus."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    def __call__(self, topic: str, payload: bytes) -> None:
        self.published.append((topic, payload))


def _windows(
    *states: EnumConsumerFlowState,
    messages_in: int | None = 397,
    messages_out: int | None = 0,
) -> tuple[ModelFlowWindowObservation, ...]:
    """Build a trailing history, oldest first, one window per state."""
    return tuple(
        ModelFlowWindowObservation(
            window_start=_EPOCH + index * _WINDOW,
            window_end=_EPOCH + (index + 1) * _WINDOW,
            flow_state=state,
            messages_in=None if state is EnumConsumerFlowState.UNKNOWN else messages_in,
            messages_out=(
                None if state is EnumConsumerFlowState.UNKNOWN else messages_out
            ),
            messages_dlq=None if state is EnumConsumerFlowState.UNKNOWN else 16,
            handler_errors=None if state is EnumConsumerFlowState.UNKNOWN else 0,
        )
        for index, state in enumerate(states)
    )


def _applied_event_payload(
    *keys: tuple[str, str],
    flow_state: EnumConsumerFlowState = EnumConsumerFlowState.STALLED,
) -> dict[str, Any]:
    """The payload shape ``handler_wiring`` actually publishes, verbatim.

    ``rows_upserted`` + ``flow_rows`` come from the projection handler's own
    return value; ``projected`` is added by the runtime emitter. Extra keys the
    emitter may add later are represented here on purpose -- the trigger model
    is the one place in this node that tolerates them, and this asserts it.
    """
    return {
        "rows_upserted": len(keys),
        "flow_rows": [
            {
                "consumer_group": consumer_group,
                "topic": topic,
                "window_start": (_EPOCH + 2 * _WINDOW).isoformat(),
                "window_end": (_EPOCH + 3 * _WINDOW).isoformat(),
                "node_id": "3f6a1f6a-0000-4000-8000-000000000001",
                "ingest_sequence": 42,
                "messages_in": 397,
                "messages_out": 0,
                "messages_dlq": 16,
                "handler_errors": 0,
                "upstream_produced": 397,
                "upstream_evidence": "OBSERVED",
                "flow_state": flow_state.value,
                "evaluated_at": (_EPOCH + 3 * _WINDOW).isoformat(),
            }
            for consumer_group, topic in keys
        ],
        "projected": True,
    }


@pytest.fixture
def slack_channel(monkeypatch: pytest.MonkeyPatch) -> str:
    """Point the contract-declared channel secret at a test value."""
    monkeypatch.setenv("SLACK_CHANNEL_ID", _TEST_CHANNEL)
    return _TEST_CHANNEL


@pytest.mark.unit
def test_the_applied_event_payload_validates_as_the_declared_input_model() -> None:
    """RED->GREEN: the shape the platform publishes is the shape declared.

    The contract's ``input_model`` is resolved dynamically here rather than
    imported by name, so this fails if the declaration and the model ever drift
    apart again -- which is precisely how the node came to be reachable and
    unable to act.
    """
    raw = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    module_path, class_name = str(raw["input_model"]).rsplit(".", 1)
    declared = getattr(importlib.import_module(module_path), class_name)

    trigger = declared.model_validate(
        _applied_event_payload(
            ("local.omnimarket.node_registration_orchestrator", "node-heartbeat.v1")
        )
    )
    assert trigger.rows_upserted == 1
    assert trigger.projected is True
    assert trigger.alerting_keys() == (
        ("local.omnimarket.node_registration_orchestrator", "node-heartbeat.v1"),
    )


@pytest.mark.unit
def test_the_trigger_ignores_keys_the_runtime_emitter_may_add() -> None:
    """An additive upstream change must not take the alert path down again.

    ``_build_projection_terminal_payload`` documents that it ADDS keys. This is
    the one model in the node that is ``extra="ignore"``; everything the node
    itself owns stays ``extra="forbid"``.
    """
    payload = _applied_event_payload(("group", "topic"))
    payload["some_future_runtime_key"] = {"added": "later"}
    trigger = ModelConsumerFlowStallAlertTrigger.model_validate(payload)
    assert trigger.alerting_keys() == (("group", "topic"),)


@pytest.mark.unit
def test_a_trigger_missing_the_batch_shape_is_refused() -> None:
    """Ignoring extras is not the same as accepting anything."""
    with pytest.raises(Exception, match="flow_rows"):
        ModelConsumerFlowStallAlertTrigger.model_validate(
            {"rows_upserted": 0, "projected": True}
        )


@pytest.mark.unit
def test_the_node_self_assembles_policy_and_windows_source_from_its_own_contract() -> (
    None
):
    """No caller supplies thresholds or a read target; the node reads both."""
    handler = HandlerConsumerFlowStallAlert(window_reader=_FakeWindowReader({}))
    assert handler.policy == load_stall_alert_policy(CONTRACT_PATH)
    assert handler.windows_source == load_windows_source(CONTRACT_PATH)


@pytest.mark.unit
def test_the_node_reads_its_own_window_history_once_per_key() -> None:
    """The read is issued by the node, keyed and bounded by its own contract."""
    source = load_windows_source(CONTRACT_PATH)
    reader = _FakeWindowReader({})
    handler = HandlerConsumerFlowStallAlert(window_reader=reader)

    handler.handle(
        ModelConsumerFlowStallAlertTrigger.model_validate(
            _applied_event_payload(("group-a", "topic-a"), ("group-b", "topic-b"))
        )
    )

    assert reader.calls == [
        ("group-a", "topic-a", source.history_windows),
        ("group-b", "topic-b", source.history_windows),
    ]


@pytest.mark.unit
def test_duplicate_rows_for_one_key_are_read_once() -> None:
    """A batch naming the same key twice must not double-read or double-alert."""
    reader = _FakeWindowReader({})
    handler = HandlerConsumerFlowStallAlert(window_reader=reader)
    handler.handle(
        ModelConsumerFlowStallAlertTrigger.model_validate(
            _applied_event_payload(("group-a", "topic-a"), ("group-a", "topic-a"))
        )
    )
    assert len(reader.calls) == 1


@pytest.mark.unit
def test_a_confirmed_stall_publishes_the_slack_command_on_the_declared_topic(
    slack_channel: str,
) -> None:
    """AC1/AC4: the alert reaches the canonical publisher, carrying the facts."""
    policy = load_stall_alert_policy(CONTRACT_PATH)
    consumer_group = "local.omnimarket.node_gateway_link_health_projection_compute"
    topic = "onex.cmd.omnibase-infra.gateway-link-health-upsert.v1"
    reader = _FakeWindowReader(
        {
            (consumer_group, topic): _windows(
                *([EnumConsumerFlowState.STALLED] * policy.confirm_windows),
                messages_in=15750,
                messages_out=0,
            )
        }
    )
    publisher = _CapturingPublisher()
    handler = HandlerConsumerFlowStallAlert(
        event_publisher=publisher, window_reader=reader
    )

    evaluation = handler.handle(
        ModelConsumerFlowStallAlertTrigger.model_validate(
            _applied_event_payload((consumer_group, topic))
        )
    )

    assert evaluation.keys_evaluated == 1
    assert evaluation.decisions[0].outcome is EnumStallAlertOutcome.FAIL_CONFIRMED_STALL
    assert evaluation.alerts_published == 1
    assert evaluation.alerts_undelivered == 0

    published_topic, raw = publisher.published[0]
    declared = set(
        yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))["event_bus"][
            "publish_topics"
        ]
    )
    assert published_topic in declared
    assert "slack-publish" in published_topic

    command = ModelSlackPublish.model_validate(json.loads(raw))
    assert command.channel == slack_channel
    assert consumer_group in command.text
    assert topic in command.text
    assert "15750" in command.text
    assert "dlq=16" in command.text
    assert str(evaluation.decisions[0].consecutive_alerting_windows) in command.text
    assert command.idempotency_key == evaluation.deliveries[0].idempotency_key


@pytest.mark.unit
def test_an_in_process_leg_with_an_empty_out_topic_does_not_alert(
    slack_channel: str,
) -> None:
    """An out-topic high-watermark of 0 is NOT evidence of a stalled leg.

    Falsified live on 2026-08-29: ``node_gateway_link_health_projection_compute``
    delivers its intent IN-PROCESS through ``IntentEffectDispatchBridge``, so
    its Kafka out-topic sits at high-watermark 0 while the node is fully alive.
    The projection's counters see that delivery and grade the window
    ``FLOWING``. This node judges the window row, so it stays silent -- a design
    that had reached for the broker instead would have paged on a healthy node.
    """
    consumer_group = "local.omnimarket.node_gateway_link_health_projection_compute"
    topic = "onex.evt.platform.node-heartbeat.v1"
    reader = _FakeWindowReader(
        {
            (consumer_group, topic): _windows(
                *([EnumConsumerFlowState.FLOWING] * 8),
                messages_in=15750,
                messages_out=15750,
            )
        }
    )
    publisher = _CapturingPublisher()
    handler = HandlerConsumerFlowStallAlert(
        event_publisher=publisher, window_reader=reader
    )

    evaluation = handler.handle(
        ModelConsumerFlowStallAlertTrigger.model_validate(
            _applied_event_payload(
                (consumer_group, topic), flow_state=EnumConsumerFlowState.FLOWING
            )
        )
    )

    assert evaluation.decisions[0].outcome is EnumStallAlertOutcome.NO_ALERT
    assert publisher.published == []


@pytest.mark.unit
def test_the_node_never_reads_a_broker_watermark() -> None:
    """Mechanical half of the same rule, over every ``.py`` file in the node.

    A high-watermark, an end-offset or a raw consumer anywhere under this node
    would mean the verdict could be taken from a topic depth. It cannot be, and
    this is what says so.
    """
    forbidden = (
        "high_watermark",
        "highwatermark",
        "end_offsets",
        "last_stable_offset",
        "AIOKafkaConsumer",
        "KafkaConsumer",
        "rpk ",
    )
    offenders: list[str] = []
    for path in sorted(NODE_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            for token in forbidden:
                if token in line:
                    offenders.append(
                        f"{path.relative_to(NODE_ROOT)}:{line_number} {token!r}"
                    )
    assert not offenders, (
        "the stall verdict must come from the projection window, never from a "
        f"broker offset (falsified premise, 2026-08-29): {offenders}"
    )


@pytest.mark.unit
def test_a_key_with_no_materialized_history_is_skipped_not_alerted() -> None:
    """A trigger naming a key the projection does not hold gets no verdict.

    Inventing one would be the confident-zero this epic exists to stop.
    """
    publisher = _CapturingPublisher()
    handler = HandlerConsumerFlowStallAlert(
        event_publisher=publisher, window_reader=_FakeWindowReader({})
    )
    evaluation = handler.handle(
        ModelConsumerFlowStallAlertTrigger.model_validate(
            _applied_event_payload(("unmaterialized", "topic"))
        )
    )
    assert evaluation.keys_evaluated == 0
    assert evaluation.decisions == ()
    assert publisher.published == []


@pytest.mark.unit
def test_an_undeliverable_alert_is_reported_rather_than_swallowed() -> None:
    """A decided-but-unpublished alert is stated by name on the terminal event.

    "The alert fired but nothing went out" is the failure this node exists to
    prevent, wearing this node's own uniform. It is not swallowed, and it is
    not DLQ'd onto a topic named for malformed input either -- that would throw
    away every other key's verdict in order to describe a delivery problem.
    """
    policy = load_stall_alert_policy(CONTRACT_PATH)
    reader = _FakeWindowReader(
        {
            ("group", "topic"): _windows(
                *([EnumConsumerFlowState.STALLED] * policy.confirm_windows)
            )
        }
    )
    handler = HandlerConsumerFlowStallAlert(event_publisher=None, window_reader=reader)

    evaluation = handler.handle(
        ModelConsumerFlowStallAlertTrigger.model_validate(
            _applied_event_payload(("group", "topic"))
        )
    )

    assert evaluation.decisions[0].outcome is EnumStallAlertOutcome.FAIL_CONFIRMED_STALL
    assert evaluation.alerts_published == 0
    assert evaluation.alerts_undelivered == 1
    delivery = evaluation.deliveries[0]
    assert delivery.published is False
    assert delivery.error is not None
    assert "event_publisher" in delivery.error


@pytest.mark.unit
def test_an_unresolvable_channel_is_reported_rather_than_guessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No channel, no post, and no invented destination."""
    monkeypatch.delenv("SLACK_CHANNEL_ID", raising=False)
    policy = load_stall_alert_policy(CONTRACT_PATH)
    reader = _FakeWindowReader(
        {
            ("group", "topic"): _windows(
                *([EnumConsumerFlowState.STALLED] * policy.confirm_windows)
            )
        }
    )
    publisher = _CapturingPublisher()
    handler = HandlerConsumerFlowStallAlert(
        event_publisher=publisher, window_reader=reader
    )

    evaluation = handler.handle(
        ModelConsumerFlowStallAlertTrigger.model_validate(
            _applied_event_payload(("group", "topic"))
        )
    )

    assert publisher.published == []
    assert evaluation.alerts_undelivered == 1
    assert evaluation.deliveries[0].error is not None


@pytest.mark.unit
def test_an_idle_consumer_on_a_quiet_topic_is_evaluated_and_stays_silent(
    slack_channel: str,
) -> None:
    """AC3 through the assembly path, not just the pure decision."""
    reader = _FakeWindowReader(
        {
            ("quiet", "topic"): _windows(
                *([EnumConsumerFlowState.IDLE] * 8), messages_in=0, messages_out=0
            )
        }
    )
    publisher = _CapturingPublisher()
    handler = HandlerConsumerFlowStallAlert(
        event_publisher=publisher, window_reader=reader
    )
    evaluation = handler.handle(
        ModelConsumerFlowStallAlertTrigger.model_validate(
            _applied_event_payload(
                ("quiet", "topic"), flow_state=EnumConsumerFlowState.IDLE
            )
        )
    )
    assert evaluation.decisions[0].outcome is EnumStallAlertOutcome.NO_ALERT
    assert publisher.published == []


@pytest.mark.unit
def test_the_key_ceiling_is_reported_not_silently_truncated(
    tmp_path: Path, slack_channel: str
) -> None:
    """A batch above ``max_keys_per_trigger`` says how many it did not read."""
    raw = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    raw["windows_source"]["max_keys_per_trigger"] = 1
    narrowed = tmp_path / "contract.yaml"
    narrowed.write_text(yaml.safe_dump(raw), encoding="utf-8")

    reader = _FakeWindowReader({})
    handler = HandlerConsumerFlowStallAlert(
        window_reader=reader, contract_path=narrowed
    )
    evaluation = handler.handle(
        ModelConsumerFlowStallAlertTrigger.model_validate(
            _applied_event_payload(("a", "a"), ("b", "b"), ("c", "c"))
        )
    )
    assert evaluation.keys_skipped == 2
    assert len(reader.calls) == 1


@pytest.mark.unit
def test_the_node_id_from_the_trigger_reaches_the_alert_payload(
    slack_channel: str,
) -> None:
    """AC4's correlation context is carried, not dropped in assembly."""
    policy = load_stall_alert_policy(CONTRACT_PATH)
    reader = _FakeWindowReader(
        {
            ("group", "topic"): _windows(
                *([EnumConsumerFlowState.STALLED] * policy.confirm_windows)
            )
        }
    )
    handler = HandlerConsumerFlowStallAlert(
        event_publisher=_CapturingPublisher(), window_reader=reader
    )
    evaluation = handler.handle(
        ModelConsumerFlowStallAlertTrigger.model_validate(
            _applied_event_payload(("group", "topic"))
        )
    )
    alert = evaluation.decisions[0].alert
    assert alert is not None
    assert alert.node_id == UUID("3f6a1f6a-0000-4000-8000-000000000001")
    assert isinstance(alert.correlation_id, UUID)


@pytest.mark.unit
def test_an_empty_applied_event_evaluates_nothing_and_does_not_fail() -> None:
    """``rows_upserted: 0`` is a real answer -- a priming tick, not an error."""
    handler = HandlerConsumerFlowStallAlert(window_reader=_FakeWindowReader({}))
    evaluation = handler.handle(
        ModelConsumerFlowStallAlertTrigger.model_validate(
            {"rows_upserted": 0, "flow_rows": [], "projected": True}
        )
    )
    assert evaluation.keys_evaluated == 0
    assert evaluation.windows_read == 0
    assert evaluation.deliveries == ()


@pytest.mark.unit
def test_the_evaluation_is_the_declared_terminal_events_payload() -> None:
    """The batch verdict is what the terminal event carries, and it validates."""
    handler = HandlerConsumerFlowStallAlert(window_reader=_FakeWindowReader({}))
    evaluation = handler.handle(
        ModelConsumerFlowStallAlertTrigger.model_validate(
            {"rows_upserted": 0, "flow_rows": [], "projected": True}
        )
    )
    encoded = json.loads(evaluation.model_dump_json())
    assert encoded["keys_evaluated"] == 0
    assert encoded["decisions"] == []
    assert encoded["deliveries"] == []
