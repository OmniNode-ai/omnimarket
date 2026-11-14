# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every model turn the loop sends carries the request's provenance (OMN-23870).

The loop runs end to end over the real delegate port; each turn's ``onex
delegate`` argv is captured, and every one of them must carry the request's
traffic class and source surface.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest
from omnibase_core.enums.enum_delegation_traffic_class import (
    EnumDelegationTrafficClass,
)

from omnimarket.delegated_code_edit.loop_ports import (
    DelegatedCodeEditPorts,
    deployed_lane_delegate_flags,
)
from omnimarket.nodes.node_delegated_code_edit_orchestrator import (
    HandlerDelegatedCodeEditOrchestrator,
    ModelDelegatedCodeEditRequest,
    ModelTurnReply,
)
from tests.nodes.node_delegated_code_edit_orchestrator.test_handler_delegated_code_edit_orchestrator import (
    FIX,
    FakePorts,
    _a,
    _reply,
    _request,
)

pytestmark = pytest.mark.unit


class _ArgvCapturingPorts(FakePorts):
    """FakePorts whose delegate turn is first sent through the real delegate port."""

    def __init__(self, tmp_path: Path, turns: list[ModelTurnReply]) -> None:
        super().__init__(turns, check_passes=(False, True))
        self.sent: list[list[str]] = []
        self._real = DelegatedCodeEditPorts(
            onex=Path("/bin/onex"),
            state_root=tmp_path / "state",
            delegate_flags=deployed_lane_delegate_flags("dev"),
            run_delegate=self._runner,
        )
        self._state = tmp_path / "state"

    def _runner(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.sent.append(list(argv))
        run_id = str(uuid.uuid4())
        run_dir = self._state / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "result.txt").write_text('{"actions": [{"tool": "ls"}]}')
        (run_dir / "receipt.json").write_text(json.dumps({"status": "success"}))
        return subprocess.CompletedProcess(argv, 0, json.dumps({"run_id": run_id}), "")

    def delegate(
        self,
        request: ModelDelegatedCodeEditRequest,
        prompt: str,
        response_contract: dict[str, object],
        turn: int,
    ) -> ModelTurnReply:
        self._real.delegate(request, prompt, response_contract, turn)
        return super().delegate(request, prompt, response_contract, turn)


def _turns() -> list[ModelTurnReply]:
    return [
        _reply(1, _a("view", path="src/m.py")),
        _reply(2, _a("write", path="src/m.py", content=FIX)),
        _reply(3, _a("finish")),
    ]


def _flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def test_turn_provenance_every_turn_carries_the_synthetic_class_and_surface(
    tmp_path: Path,
) -> None:
    ports = _ArgvCapturingPorts(tmp_path, _turns())
    request = _request(
        traffic_class=EnumDelegationTrafficClass.SYNTHETIC,
        source_surface="trial-driver",
        caller_lane="lane-x",
    )
    HandlerDelegatedCodeEditOrchestrator(ports).run(request)
    assert len(ports.sent) >= 3
    for argv in ports.sent:
        assert _flag(argv, "--traffic-class") == "synthetic"
        assert _flag(argv, "--source-surface") == "trial-driver"


def test_turn_provenance_a_request_naming_none_sends_unclassified_on_every_turn(
    tmp_path: Path,
) -> None:
    ports = _ArgvCapturingPorts(tmp_path, _turns())
    HandlerDelegatedCodeEditOrchestrator(ports).run(_request())
    assert len(ports.sent) >= 3
    for argv in ports.sent:
        assert _flag(argv, "--traffic-class") == "unclassified"
        assert "--source-surface" not in argv


def test_turn_provenance_survives_the_in_process_retry(tmp_path: Path) -> None:
    state = tmp_path / "state"
    seen: list[list[str]] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        seen.append(list(argv))
        if "deployed-lane" in argv:
            raise subprocess.TimeoutExpired(argv, 900)
        run_id = str(uuid.uuid4())
        run_dir = state / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "result.txt").write_text('{"actions": [{"tool": "ls"}]}')
        (run_dir / "receipt.json").write_text(json.dumps({"status": "success"}))
        return subprocess.CompletedProcess(argv, 0, json.dumps({"run_id": run_id}), "")

    ports = DelegatedCodeEditPorts(
        onex=Path("/bin/onex"),
        state_root=state,
        delegate_flags=deployed_lane_delegate_flags("dev"),
        run_delegate=runner,
    )
    ports.delegate(
        _request(
            traffic_class=EnumDelegationTrafficClass.SYNTHETIC,
            source_surface="trial-driver",
        ),
        "P",
        {"type": "object"},
        1,
    )
    assert len(seen) == 2
    for argv in seen:
        assert _flag(argv, "--traffic-class") == "synthetic"
        assert _flag(argv, "--source-surface") == "trial-driver"
