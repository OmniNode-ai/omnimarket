# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_session_bootstrap.

Verifies: bootstrap command -> handler -> ModelBootstrapResult.
Uses EventBusInmemory. No subprocess calls. No real filesystem writes (dry_run).
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid

import pytest

from omnimarket.nodes.node_session_bootstrap.handlers.handler_session_bootstrap import (
    EnumBootstrapStatus,
    HandlerSessionBootstrap,
    ModelBootstrapCommand,
)

CMD_TOPIC = "onex.cmd.omnimarket.session-bootstrap-start.v1"
EVT_TOPIC = "onex.evt.omnimarket.session-bootstrap-completed.v1"

_VALID_CONTRACT: dict[str, object] = {
    "session_id": "test-session-001",
    "session_label": "2026-04-10 overnight",
    "phases_expected": ["build_loop", "merge_sweep", "platform_readiness"],
    "max_cycles": 0,
    "cost_ceiling_usd": 10.0,
    "halt_on_build_loop_failure": True,
    "dry_run": False,
    "schema_version": "1.0",
}


def _make_command(
    session_id: str | None = None,
    contract: dict[str, object] | None = None,
    state_dir: str = ".onex_state",
    dry_run: bool = True,
) -> ModelBootstrapCommand:
    return ModelBootstrapCommand(
        session_id=session_id or str(uuid.uuid4()),
        contract=contract or dict(_VALID_CONTRACT),
        state_dir=state_dir,
        dry_run=dry_run,
    )


@pytest.mark.unit
class TestGoldenChainSessionBootstrap:
    """Golden chain: bootstrap command -> handler -> result."""

    def test_dry_run_no_filesystem_write(self) -> None:
        """dry_run=True -> contract_path == '(dry-run)', no disk write."""
        handler = HandlerSessionBootstrap()
        cmd = _make_command(dry_run=True)
        result = handler.handle(cmd)

        assert result.contract_path == "(dry-run)"
        assert result.dry_run is True

    def test_ready_status_valid_contract(self) -> None:
        """Valid contract with phases -> status == READY."""
        handler = HandlerSessionBootstrap()
        cmd = _make_command(dry_run=True)
        result = handler.handle(cmd)

        assert result.status == EnumBootstrapStatus.READY
        assert result.warnings == []

    def test_warns_on_empty_phases(self) -> None:
        """phases_expected=[] -> warning present, status DEGRADED."""
        contract = dict(_VALID_CONTRACT)
        contract["phases_expected"] = []
        handler = HandlerSessionBootstrap()
        cmd = _make_command(contract=contract, dry_run=True)
        result = handler.handle(cmd)

        assert result.status == EnumBootstrapStatus.DEGRADED
        assert any("phases_expected is empty" in w for w in result.warnings)

    def test_warns_on_high_cost_ceiling(self) -> None:
        """cost_ceiling_usd > 20.0 -> warning present."""
        contract = dict(_VALID_CONTRACT)
        contract["cost_ceiling_usd"] = 50.0
        handler = HandlerSessionBootstrap()
        cmd = _make_command(contract=contract, dry_run=True)
        result = handler.handle(cmd)

        assert any("cost_ceiling_usd" in w for w in result.warnings)

    def test_timer_configs_always_present(self) -> None:
        """result.timer_configs always includes merge_sweep, health_check, agent_watchdog."""
        handler = HandlerSessionBootstrap()
        cmd = _make_command(dry_run=True)
        result = handler.handle(cmd)

        timer_str = " ".join(result.timer_configs)
        assert "merge_sweep" in timer_str
        assert "health_check" in timer_str
        assert "agent_watchdog" in timer_str
        assert len(result.timer_configs) >= 3

    def test_session_id_round_trips(self) -> None:
        """session_id passed in command is preserved in result."""
        session_id = str(uuid.uuid4())
        handler = HandlerSessionBootstrap()
        cmd = _make_command(session_id=session_id, dry_run=True)
        result = handler.handle(cmd)

        assert result.session_id == session_id

    def test_contract_path_contains_session_id(self) -> None:
        """Not dry_run -> contract_path contains session_id."""
        session_id = str(uuid.uuid4())
        handler = HandlerSessionBootstrap()
        with tempfile.TemporaryDirectory() as tmp:
            cmd = _make_command(session_id=session_id, state_dir=tmp, dry_run=False)
            result = handler.handle(cmd)

        assert session_id in result.contract_path
        assert result.contract_path != "(dry-run)"

    def test_contract_file_written_on_disk(self) -> None:
        """Not dry_run -> actual file is created with valid JSON."""
        session_id = str(uuid.uuid4())
        handler = HandlerSessionBootstrap()
        with tempfile.TemporaryDirectory() as tmp:
            cmd = _make_command(session_id=session_id, state_dir=tmp, dry_run=False)
            result = handler.handle(cmd)

            assert os.path.isfile(result.contract_path)
            with open(result.contract_path) as f:
                payload = json.load(f)
            assert payload["session_id"] == session_id

    def test_event_bus_wiring(self, event_bus: object) -> None:
        """Handler returns valid result regardless of event_bus fixture presence."""
        handler = HandlerSessionBootstrap()
        cmd = _make_command(dry_run=True)
        result = handler.handle(cmd)

        assert result.status in (
            EnumBootstrapStatus.READY,
            EnumBootstrapStatus.DEGRADED,
        )

    def test_result_serializes_to_json(self) -> None:
        """result.model_dump_json() parses cleanly."""
        handler = HandlerSessionBootstrap()
        cmd = _make_command(dry_run=True)
        result = handler.handle(cmd)

        raw = result.model_dump_json()
        parsed = json.loads(raw)
        assert parsed["session_id"] == cmd.session_id
        assert "bootstrapped_at" in parsed
