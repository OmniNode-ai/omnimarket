# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for the host observer node (OMN-20801).

The chain is the one the contract declares: a poll command arrives on the bus as
JSON, the handler reads the overlay's evidence, and the events it journals leave
on topics the contract publishes.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_automation_run_observer_effect.handlers import (
    HandlerAutomationRunObserver,
)
from omnimarket.nodes.node_automation_run_observer_effect.models import (
    ModelAutomationRunObserverRequest,
    ModelAutomationRunObserverResult,
)

pytestmark = pytest.mark.unit

NODE = "node_automation_run_observer_effect"
CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / NODE
    / "contract.yaml"
)
COMMAND_TOPIC = "onex.cmd.omnimarket.automation-run-observer-poll-requested.v1"
PUBLISHED = {
    "onex.evt.omnimarket.automation-run-observed.v1",
    "onex.evt.omnimarket.automation-heartbeat.v1",
    "onex.evt.omnimarket.automation-liveness-verdict.v1",
    "onex.evt.omnimarket.automation-run-observer-polled.v1",
}


class RecordingSink:
    def __init__(self) -> None:
        self.topics: list[str] = []

    def publish(
        self, topic: str, payload: dict[str, object], *, key: str, event_id: str
    ) -> None:
        self.topics.append(topic)


def test_golden_chain_poll_command_to_declared_topics(tmp_path: Path) -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert set(contract["event_bus"]["publish_topics"]) == PUBLISHED
    assert contract["terminal_event"] == (
        "onex.evt.omnimarket.automation-run-observer-polled.v1"
    )

    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text(
        json.dumps(
            {
                "started_at": "2026-10-10T11:00:00+00:00",
                "finished_at": "2026-10-10T11:00:30+00:00",
                "exit_code": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "schema_version": "automation-liveness-overlay/v1",
                "processes": [
                    {
                        "process_id": "host-a/job",
                        "owner_repo": "example-owner/example-repo",
                        "host": "host-a",
                        "trigger": {"kind": "launchd-interval", "native_id": "x.job"},
                        "expected_interval_seconds": 300,
                        "alarm_deadline_seconds": 1800,
                        "max_runtime_seconds": 240,
                        "emitter": "observer",
                        "run_record": "per-run",
                        "evidence": {
                            "source": "receipts_file",
                            "locator": str(receipts),
                            "completion_record": "exit_code",
                        },
                        "real_work": "placed",
                        "demand": "none",
                        "positive_control": {"kind": "class_drill", "ref": "stop it"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    command = ModelAutomationRunObserverRequest.model_validate_json(
        json.dumps(
            {
                "host": "host-a",
                "overlay_path": str(overlay),
                "state_dir": str(tmp_path / "state"),
                "now": datetime(2026, 10, 10, 12, 0, tzinfo=UTC).isoformat(),
            }
        )
    )
    sink = RecordingSink()
    result = HandlerAutomationRunObserver(sink=sink).handle(command)
    assert isinstance(result, ModelAutomationRunObserverResult)
    assert result.runs_emitted == 1
    assert result.journal_backlog == 0
    assert set(sink.topics) <= PUBLISHED
    assert sink.topics.count("onex.evt.omnimarket.automation-run-observed.v1") == 1
    assert "onex.evt.omnimarket.automation-heartbeat.v1" in sink.topics


def test_error_chain_malformed_poll_command_never_reaches_the_handler() -> None:
    with pytest.raises(ValidationError) as caught:
        ModelAutomationRunObserverRequest.model_validate(
            {"host": "Host A", "overlay_path": "x"}
        )
    assert {err["loc"][0] for err in caught.value.errors()} >= {
        "host",
        "state_dir",
        "now",
    }


def test_error_chain_unreadable_overlay_is_refused_not_read_as_empty(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError):
        HandlerAutomationRunObserver(sink=RecordingSink()).handle(
            ModelAutomationRunObserverRequest(
                host="host-a",
                overlay_path=str(tmp_path / "missing.yaml"),
                state_dir=str(tmp_path / "state"),
                now=datetime(2026, 10, 10, 12, 0, tzinfo=UTC),
            )
        )


@pytest.mark.asyncio
async def test_observer_runtime_dispatch_terminal_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raw bus commands reach the shipped handler and terminalize on re-read."""
    from omnimarket.nodes.node_automation_run_observer_effect.handlers import (
        handler_automation_run_observer,
    )
    from tests.chains.test_event_chain_gate import ChainCase, _run_chain
    from tests.test_automation_run_observer_effect import (
        T0,
        FakeSink,
        receipt,
        receipts_overlay,
    )

    overlay, receipts = receipts_overlay(tmp_path)
    receipts.write_text(receipt(0), encoding="utf-8")
    sink = FakeSink()
    monkeypatch.setattr(
        handler_automation_run_observer, "KafkaObserverEventSink", lambda: sink
    )
    case = ChainCase(
        chain_id="automation-run-observer",
        node_dir=NODE,
        entry_topic=COMMAND_TOPIC,
        terminal_topic="onex.evt.omnimarket.automation-run-observer-polled.v1",
        terminal_type_name="ModelAutomationRunObserverResult",
        wire_payload={
            "host": "host-a",
            "overlay_path": overlay,
            "state_dir": str(tmp_path / "state"),
            "now": T0.isoformat(),
        },
    )
    for expected_runs in (1, 0):
        run = await _run_chain(case)
        assert run.prepared.quarantine_reason is None
        assert run.quarantine_messages == []
        assert len(run.terminal_messages) == 1
        terminal = json.loads(run.terminal_messages[0])
        assert terminal["payload"]["runs_emitted"] == expected_runs
        assert terminal["payload"]["journal_backlog"] == 0
    assert len(sink.of("onex.evt.omnimarket.automation-run-observed.v1")) == 1
