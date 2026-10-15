# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Morning phases run as bus lab-work while agent_delegation is pending a capability.

A delegation request for ``agent_delegation`` is refused by the task-class
contract (``routing_availability: pending_capability``), so the delegation
gateway fails fast with a typed error naming the missing capability and its
tracking, and the default gateway submits each phase as a lab work unit that
runs a headless agent session on a placed pool host.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import cast

import pytest
from pydantic import JsonValue

from omnimarket.lab_work.placement import (
    EnumPlacementDecision,
    EnumRefusalReason,
    ModelPlacement,
)
from omnimarket.nodes.node_lab_work_unit_effect.models import (
    EnumLabWorkUnitStatus,
    ModelHostCapacityAdvertisement,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_morning_ground_state_orchestrator.handlers import (
    phase_agent_stdin,
)
from omnimarket.nodes.node_morning_ground_state_orchestrator.handlers.handler_morning_ground_state import (
    HandlerMorningGroundState,
)
from omnimarket.nodes.node_morning_ground_state_orchestrator.handlers.handler_morning_phase_bus import (
    HandlerMorningPhaseBus,
    MorningPhaseCapabilityPendingError,
)
from omnimarket.nodes.node_morning_ground_state_orchestrator.handlers.handler_morning_phase_lab_work import (
    AGENT_LAUNCHER,
    HandlerMorningPhaseLabWork,
    MorningPhaseLabWorkError,
)
from omnimarket.nodes.node_morning_ground_state_orchestrator.models import (
    ModelMorningPhaseRequest,
)

from .helpers import public_overlay, request

REPO_ROOT = Path(__file__).resolve().parents[3]
SHA = "28219cf929b32bf51e0a83ac21d89caeed1a27d5"
TARGET: dict[str, JsonValue] = {
    "labWork": {
        "repo": "OmniNode-ai/omnimarket",
        "commit_sha": SHA,
        "lane": "morning-ground-state",
    }
}
STUB: dict[str, JsonValue] = {
    "state": "stub",
    "detail": "fixture",
    "prs": [],
    "residuals": [],
    "delegation": {"delegated": 1, "runs": ["fixture"], "reason": ""},
}


class NeverCalledRpc:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def send_request(
        self,
        instance_name: str,
        payload: dict[str, object],
        timeout_seconds: int | None = None,
    ) -> dict[str, object]:
        self.calls.append(payload)
        raise RuntimeError("Request failed: Request failed")


class Caller:
    """Records the units the gateway sends; answers with a fixed receipt."""

    def __init__(
        self,
        tail: str = "",
        *,
        status: EnumLabWorkUnitStatus = EnumLabWorkUnitStatus.COMPLETED,
        exit_code: int | None = 0,
        placed: bool = True,
    ) -> None:
        self.tail = tail
        self.status = status
        self.exit_code = exit_code
        self.placed = placed
        self.sent: list[ModelLabWorkUnitRequest] = []
        self.needs: list[tuple[str, ...]] = []
        self.started = 0

    async def start(self) -> None:
        self.started += 1

    async def collect(
        self, window_seconds: float
    ) -> list[ModelHostCapacityAdvertisement]:
        return []

    def place(
        self, *, need_tools: tuple[str, ...] = (), only_host: str | None = None
    ) -> ModelPlacement:
        self.needs.append(need_tools)
        if not self.placed:
            return ModelPlacement(
                decision=EnumPlacementDecision.REFUSED,
                refusal=EnumRefusalReason.COULD_NOT_CHECK,
                verdicts=[],
            )
        return ModelPlacement(
            decision=EnumPlacementDecision.PLACED, host_name="h101", verdicts=[]
        )

    async def dispatch(self, unit: ModelLabWorkUnitRequest) -> ModelLabWorkUnitReceipt:
        self.sent.append(unit)
        return ModelLabWorkUnitReceipt(
            work_unit_id=unit.work_unit_id,
            target_host=unit.target_host,
            host=unit.target_host,
            repo=unit.repo,
            commit_sha=unit.commit_sha,
            lane=unit.lane,
            kind=unit.kind,
            status=self.status,
            exit_code=self.exit_code,
            output_tail=self.tail,
        )


def _phase(label: str = "ground-state") -> ModelMorningPhaseRequest:
    return ModelMorningPhaseRequest(
        label=label,
        phase="GroundState",
        model="opus",
        effort="high",
        prompt="fixture brief",
        schema_definition={"type": "object"},
    )


@pytest.mark.unit
def test_a_refused_agent_delegation_raises_the_typed_pending_capability_error() -> None:
    rpc = NeverCalledRpc()
    gateway = HandlerMorningPhaseBus(rpc=rpc, overlay=public_overlay())
    with pytest.raises(MorningPhaseCapabilityPendingError) as caught:
        asyncio.run(gateway.run(request(), _phase()))
    error = caught.value
    assert error.task_class == "agent_delegation"
    assert error.missing_capability == "agent_orchestration"
    assert "OMN-15961" in error.tracking
    assert "OMN-15961" in str(error)
    assert "pending_capability" in str(error)
    # Fail fast: nothing is published to wait out the reply budget.
    assert rpc.calls == []


@pytest.mark.unit
def test_the_default_gateway_runs_phases_as_lab_work() -> None:
    handler = HandlerMorningGroundState(None, overlay=public_overlay())
    assert isinstance(handler.gateway, HandlerMorningPhaseLabWork)


@pytest.mark.unit
def test_a_phase_is_sent_as_a_headless_agent_lab_work_unit() -> None:
    caller = Caller("agent log line\n{not json\n" + json.dumps(STUB) + "\n")
    gateway = HandlerMorningPhaseLabWork(caller=caller, overlay=public_overlay())
    result = asyncio.run(gateway.run(request(TARGET), _phase()))
    assert result == STUB
    assert caller.started == 1
    assert caller.needs == [("claude", "claude-login")]
    (unit,) = caller.sent
    assert unit.target_host == "h101"
    assert unit.repo == "OmniNode-ai/omnimarket"
    assert unit.commit_sha == SHA
    assert unit.lane == "morning-ground-state"
    assert unit.need_tools == ["claude", "claude-login"]
    # No shell on a pool host: python3 runs the node's launcher from the checkout.
    assert unit.argv[0] == "python3"
    assert unit.argv[1] == AGENT_LAUNCHER
    assert (REPO_ROOT / AGENT_LAUNCHER).is_file()
    assert unit.argv[2] == "opus"
    assert "fixture brief" in "".join(unit.argv)
    # A rerun of the same phase of the same run is the same unit id.
    again = Caller(json.dumps(STUB))
    asyncio.run(
        HandlerMorningPhaseLabWork(caller=again, overlay=public_overlay()).run(
            request(TARGET), _phase()
        )
    )
    assert again.sent[0].work_unit_id == unit.work_unit_id


@pytest.mark.unit
def test_a_long_brief_is_split_across_bounded_argv_words() -> None:
    caller = Caller(json.dumps(STUB))
    phase = _phase().model_copy(update={"prompt": "x" * 50_000})
    asyncio.run(
        HandlerMorningPhaseLabWork(caller=caller, overlay=public_overlay()).run(
            request(TARGET), phase
        )
    )
    unit = caller.sent[0]
    assert max(len(word) for word in unit.argv) <= 20_000
    assert "x" * 50_000 in "".join(unit.argv)


@pytest.mark.unit
def test_a_run_without_a_lab_work_target_is_refused_before_any_unit() -> None:
    caller = Caller(json.dumps(STUB))
    with pytest.raises(MorningPhaseLabWorkError, match="labWork"):
        asyncio.run(
            HandlerMorningPhaseLabWork(caller=caller, overlay=public_overlay()).run(
                request(), _phase()
            )
        )
    assert caller.sent == []


@pytest.mark.unit
def test_no_placeable_host_is_a_typed_refusal() -> None:
    caller = Caller(json.dumps(STUB), placed=False)
    with pytest.raises(MorningPhaseLabWorkError, match="no pool host"):
        asyncio.run(
            HandlerMorningPhaseLabWork(caller=caller, overlay=public_overlay()).run(
                request(TARGET), _phase()
            )
        )
    assert caller.sent == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "exit_code"),
    [
        (EnumLabWorkUnitStatus.COMPLETED, 1),
        (EnumLabWorkUnitStatus.TIMED_OUT, None),
        (EnumLabWorkUnitStatus.INFRA_ERROR, None),
    ],
)
def test_a_unit_that_did_not_exit_zero_is_a_typed_failure(
    status: EnumLabWorkUnitStatus, exit_code: int | None
) -> None:
    caller = Caller(json.dumps(STUB), status=status, exit_code=exit_code)
    with pytest.raises(MorningPhaseLabWorkError, match=status.value):
        asyncio.run(
            HandlerMorningPhaseLabWork(caller=caller, overlay=public_overlay()).run(
                request(TARGET), _phase()
            )
        )


@pytest.mark.unit
def test_a_tail_with_no_json_result_is_a_typed_failure() -> None:
    caller = Caller("the agent wrote prose and no result")
    with pytest.raises(MorningPhaseLabWorkError, match="no JSON result"):
        asyncio.run(
            HandlerMorningPhaseLabWork(caller=caller, overlay=public_overlay()).run(
                request(TARGET), _phase()
            )
        )


@pytest.mark.unit
def test_the_typed_phase_schema_still_applies_to_lab_work_results() -> None:
    caller = Caller(json.dumps({"state": 1}))
    with pytest.raises(ValueError, match="validation error"):
        asyncio.run(
            HandlerMorningPhaseLabWork(caller=caller, overlay=public_overlay()).run(
                request(TARGET), _phase()
            )
        )


@pytest.mark.unit
def test_reconcile_runs_through_lab_work_too() -> None:
    caller = Caller("{}")
    asyncio.run(
        HandlerMorningPhaseLabWork(caller=caller, overlay=public_overlay()).reconcile(
            request(cast(dict[str, JsonValue], {**TARGET, "liveLanes": []}))
        )
    )
    assert "ledger-reconcile-tool --apply" in "".join(caller.sent[0].argv)


@pytest.mark.unit
def test_the_launcher_hands_the_joined_brief_to_the_agent_on_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[list[str], str]] = []

    def run(
        args: list[str], *, input: str, text: bool, check: bool
    ) -> subprocess.CompletedProcess[str]:
        seen.append((args, input))
        return subprocess.CompletedProcess(args, 3)

    monkeypatch.setattr(subprocess, "run", run)
    assert phase_agent_stdin.main(["sonnet", "first ", "second"]) == 3
    ((args, brief),) = seen
    assert args[:2] == ["claude", "-p"]
    assert args[-2:] == ["--model", "sonnet"]
    assert brief == "first second"
    assert phase_agent_stdin.main(["sonnet"]) == 64
