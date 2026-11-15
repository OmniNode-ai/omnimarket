# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime-entry proof for node_goal_verification_orchestrator [OMN-24236].

The outcome the orchestrator returns (``ModelGoalVerificationOutcome``) is a
record of what was decided, so the deployed runtime must publish it on the
record topic the contract declares -- ``goal-verification-decided`` -- and not
on the verifier's start command.

The deployed runtime publishes a handler's returned model through the contract
result applier (``build_contract_result_applier``), wired the way
omnibase_infra's auto-wiring wires it: the fallback output topic from the
contract's ``terminal_event`` (else the first publish topic), the per-class
routing map from ``published_events``. These tests drive that wiring over the
REAL contract file with the handler's result shape, so a contract that cannot
reach its own decided topic is caught here rather than dead-lettering on the
bus the way the start-command misroute did.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.enums import EnumDispatchStatus
from omnibase_infra.models.dispatch.model_dispatch_result import ModelDispatchResult
from omnibase_infra.protocols import ProtocolEventBusLike
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _select_dispatch_result_output_topic,
)
from omnibase_infra.runtime.event_bus_subcontract_wiring import (
    load_published_events_map,
)
from omnibase_infra.runtime.service_dispatch_result_applier import (
    build_contract_result_applier,
)
from pydantic import ValidationError

from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_goal_verification_orchestrator.models.model_goal_verification import (
    EnumGoalVerificationAction,
    ModelGoalVerificationDecision,
    ModelGoalVerificationOutcome,
)
from tests.nodes.node_goal_verification_orchestrator.support import GOAL, REV1

pytestmark = pytest.mark.unit

DECIDED = "onex.evt.omnimarket.goal-verification-decided.v1"
START = "onex.cmd.omnimarket.dod-verify-start.v1"
_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_goal_verification_orchestrator"
    / "contract.yaml"
)


def _outcome() -> ModelGoalVerificationOutcome:
    return ModelGoalVerificationOutcome(
        decisions=(
            ModelGoalVerificationDecision(
                goal_id=GOAL,
                contract_revision=REV1,
                action=EnumGoalVerificationAction.CONTRACT_UNREAD,
                detail="x",
            ),
        )
    )


def _wired_applier(monkeypatch: pytest.MonkeyPatch, contract_path: Path) -> Any:
    """Wire the applier the way omnibase_infra's auto-wiring wires a contract."""
    monkeypatch.delenv("ONEX_ACTIVE_RUNTIME_PACKAGES", raising=False)
    manifest = discover_contracts_from_paths([contract_path])
    assert not manifest.errors, manifest.errors
    assert not manifest.skips, manifest.skips
    contract = manifest.contracts[0]
    return build_contract_result_applier(
        event_bus=AsyncMock(spec=ProtocolEventBusLike),
        contract_path=contract_path,
        publish_topics=contract.event_bus.publish_topics,
        output_topic=_select_dispatch_result_output_topic(contract),
        output_topic_map=load_published_events_map(contract_path),
        allowed_output_topics=contract.event_bus.publish_topics,
    )


async def _published_topic(applier: Any, outcome: ModelGoalVerificationOutcome) -> str:
    correlation_id = uuid4()
    await applier.apply(
        ModelDispatchResult(
            status=EnumDispatchStatus.SUCCESS,
            topic=START,
            started_at=datetime.now(UTC),
            correlation_id=correlation_id,
            dispatcher_id="omn24236-seam",
            output_events=[outcome],
        ),
        correlation_id=correlation_id,
    )
    bus = applier._event_bus
    bus.publish_envelope.assert_awaited_once()
    return str(bus.publish_envelope.await_args.kwargs["topic"])


def test_contract_names_its_own_decided_topic() -> None:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))

    assert contract["terminal_event"] == DECIDED
    publish_topics = contract["event_bus"]["publish_topics"]
    assert publish_topics[0] == DECIDED
    assert START in publish_topics
    published = {
        entry["event_type"]: entry["topic"] for entry in contract["published_events"]
    }
    assert published == {"GoalVerificationOutcome": DECIDED}
    assert DECIDED in contract["externally_consumed_topics"]


@pytest.mark.asyncio
async def test_runtime_publishes_the_outcome_on_the_decided_topic_not_the_start_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        await _published_topic(_wired_applier(monkeypatch, _CONTRACT_PATH), _outcome())
        == DECIDED
    )


@pytest.mark.asyncio
async def test_failing_control_a_contract_without_terminal_event_publishes_on_the_start_topic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pins the misroute OMN-24236 removes: with no terminal_event and no
    published_events the applier's fallback is the FIRST publish topic, so the
    outcome record was published as the verifier's start command and rejected
    by node_dod_verify on both ends."""
    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    del contract["terminal_event"]
    del contract["published_events"]
    contract["event_bus"]["publish_topics"].remove(DECIDED)
    stripped_path = tmp_path / "contract.yaml"
    stripped_path.write_text(yaml.safe_dump(contract), encoding="utf-8")

    assert (
        await _published_topic(_wired_applier(monkeypatch, stripped_path), _outcome())
        == START
    )


def test_the_outcome_is_not_a_start_command() -> None:
    with pytest.raises(ValidationError) as exc_info:
        ModelDodVerifyStartCommand.model_validate(_outcome().model_dump(mode="json"))

    message = str(exc_info.value)
    assert "ticket_id" in message
    assert "decisions" in message
