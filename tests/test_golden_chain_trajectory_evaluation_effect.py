# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_trajectory_evaluation_effect (OMN-20087).

Each outcome class is routed, as the runtime's result applier does, by its
class name with the ``Model`` prefix removed through the contract's
``published_events`` map, and lands only on its own topic of an in-memory bus.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import BaseModel

import omnimarket.nodes.node_trajectory_evaluation_effect as node_pkg
from omnimarket.nodes.node_trajectory_evaluation_effect.evaluators.evaluator_in_memory import (
    EvaluatorInMemory,
)
from omnimarket.nodes.node_trajectory_evaluation_effect.handlers.handler_trajectory_evaluation import (
    HandlerTrajectoryEvaluation,
)
from omnimarket.nodes.node_trajectory_evaluation_effect.models.model_trajectory_evaluation import (
    ModelTrajectoryEvaluationAccepted,
    ModelTrajectoryEvaluationFailed,
    ModelTrajectoryEvaluationRefused,
    ModelTrajectoryEvaluationStatus,
)
from tests.unit.nodes.node_trajectory_evaluation_effect.fakes import (
    SETTING,
    Github,
    poll_cmd,
    submit_cmd,
)

pytestmark = pytest.mark.unit

_CONTRACT = Path(node_pkg.__file__).parent / "contract.yaml"


def _contract() -> dict[str, Any]:
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _topic_for(outcome: BaseModel) -> str:
    event_type = type(outcome).__name__.removeprefix("Model")
    topics = [
        e["topic"]
        for e in _contract()["published_events"]
        if e["event_type"] == event_type
    ]
    assert len(topics) == 1, event_type
    return str(topics[0])


def _handler(setting: str, github: Github | None = None) -> HandlerTrajectoryEvaluation:
    return HandlerTrajectoryEvaluation(
        transport=(github or Github()).transport(),
        evaluator=EvaluatorInMemory(terminal_after_reads=1),
        environ={SETTING: setting},
    )


async def test_each_outcome_class_lands_only_on_its_own_topic() -> None:
    accepted = await _handler("in_memory").handle(submit_cmd())
    refused = await _handler("off").handle(submit_cmd())
    failed = await _handler("in_memory", Github(lambda _r: httpx.Response(500))).handle(
        submit_cmd()
    )
    status_handler = _handler("in_memory")
    first = await status_handler.handle(submit_cmd())
    assert isinstance(first, ModelTrajectoryEvaluationAccepted)
    status = await status_handler.handle(
        poll_cmd(backend_receipt_id=first.backend_receipt_id)
    )

    expected: dict[type[BaseModel], BaseModel] = {
        ModelTrajectoryEvaluationAccepted: accepted,
        ModelTrajectoryEvaluationRefused: refused,
        ModelTrajectoryEvaluationFailed: failed,
        ModelTrajectoryEvaluationStatus: status,
    }
    for cls, outcome in expected.items():
        assert type(outcome) is cls

    bus = EventBusInmemory()
    await bus.start()
    topics = {cls: _topic_for(out) for cls, out in expected.items()}
    assert len(set(topics.values())) == 4
    for cls, outcome in expected.items():
        await bus.publish(
            topics[cls], key=None, value=outcome.model_dump_json().encode()
        )
    for cls, topic in topics.items():
        history = await bus.get_event_history(topic=topic)
        assert len(history) == 1
        assert cls.__name__.removeprefix("Model").replace(
            "TrajectoryEvaluation", ""
        ) in ("Accepted", "Refused", "Failed", "Status")
        assert type(expected[cls]).__name__ == cls.__name__
        assert expected[cls].model_dump_json().encode() == history[0].value
    await bus.close()


def test_contract_wires_commands_outcomes_and_dead_letters() -> None:
    contract = _contract()
    bus = contract["event_bus"]
    assert sorted(bus["subscribe_topics"]) == [
        "onex.cmd.omnimarket.trajectory-evaluation-poll.v1",
        "onex.cmd.omnimarket.trajectory-evaluation-submit.v1",
    ]
    published = {e["topic"] for e in contract["published_events"]}
    assert set(bus["dlq_topics"]) == {
        "onex.dlq.omnimarket.trajectory-evaluation-submit.v1",
        "onex.dlq.omnimarket.trajectory-evaluation-poll.v1",
    }
    assert published <= set(contract["externally_consumed_topics"])
    assert contract["node_type"] == "EFFECT_GENERIC"
    assert contract["lifecycle"] == "experimental"
    routes = {
        h["operation"]: h["topic"] for h in contract["handler_routing"]["handlers"]
    }
    assert routes == {
        "trajectory_evaluation.submit": "onex.cmd.omnimarket.trajectory-evaluation-submit.v1",
        "trajectory_evaluation.poll": "onex.cmd.omnimarket.trajectory-evaluation-poll.v1",
    }
