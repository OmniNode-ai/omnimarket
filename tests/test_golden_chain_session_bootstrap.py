# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_session_bootstrap (Rev 7).

Verifies: bootstrap command -> handler -> ModelBootstrapResult.
No subprocess calls. No real filesystem writes except in explicit disk tests.
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

CMD_TOPIC = "onex.cmd.omnimarket.session-bootstrap-start.v2"
EVT_TOPIC = "onex.evt.omnimarket.session-bootstrap-completed.v2"

_VALID_CONTRACT: dict[str, object] = {
    "session_id": "test-session-001",
    "session_label": "2026-04-12 bootstrap rev7",
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
    session_mode: str = "build",
) -> ModelBootstrapCommand:
    return ModelBootstrapCommand(
        session_id=session_id or str(uuid.uuid4()),
        contract=contract or dict(_VALID_CONTRACT),
        state_dir=state_dir,
        dry_run=dry_run,
        session_mode=session_mode,
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
        """Valid contract with phases and build mode -> status == READY."""
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

    # ------------------------------------------------------------------
    # Rev 7 additions
    # ------------------------------------------------------------------

    def test_dry_run_build_mode_records_cron_placeholder(self) -> None:
        """dry_run=True in build mode -> crons_registered contains dry-run placeholder."""
        handler = HandlerSessionBootstrap()
        cmd = _make_command(dry_run=True, session_mode="build")
        result = handler.handle(cmd)

        assert any("dry-run" in c for c in result.crons_registered)

    def test_reporting_mode_no_crons_registered(self) -> None:
        """reporting mode has no phase-1 active crons -> crons_registered is empty."""
        handler = HandlerSessionBootstrap()
        cmd = _make_command(dry_run=True, session_mode="reporting")
        result = handler.handle(cmd)

        assert result.crons_registered == []

    def test_invalid_session_mode_degrades(self) -> None:
        """Unknown session_mode -> DEGRADED with warning."""
        handler = HandlerSessionBootstrap()
        cmd = _make_command(dry_run=True, session_mode="unknown-mode")
        result = handler.handle(cmd)

        assert result.status == EnumBootstrapStatus.DEGRADED
        assert any("session_mode" in w for w in result.warnings)

    def test_cron_skip_when_already_registered(self) -> None:
        """C5: If cron already in CronList, CronCreate is NOT called; ID recorded."""
        existing = [{"name": "build-dispatch-pulse", "id": "existing-job-42"}]
        create_calls: list[dict[str, object]] = []

        def fake_list() -> list[dict[str, str]]:
            return existing  # type: ignore[return-value]

        def fake_create(**kwargs: object) -> str:
            create_calls.append(dict(kwargs))
            return "new-job-id"

        handler = HandlerSessionBootstrap(
            cron_list_fn=fake_list, cron_create_fn=fake_create
        )
        cmd = _make_command(dry_run=False, session_mode="build")
        with tempfile.TemporaryDirectory() as tmp:
            cmd = ModelBootstrapCommand(
                session_id=str(uuid.uuid4()),
                contract=_VALID_CONTRACT,  # type: ignore[arg-type]
                state_dir=tmp,
                dry_run=False,
                session_mode="build",
            )
            result = handler.handle(cmd)

        assert create_calls == [], (
            "CronCreate must NOT be called when cron already exists"
        )
        assert "existing-job-42" in result.crons_registered

    def test_cron_create_called_when_not_registered(self) -> None:
        """C5: If cron not in CronList, CronCreate IS called and job ID recorded."""
        create_calls: list[dict[str, object]] = []

        def fake_list() -> list[dict[str, str]]:
            return []

        def fake_create(cron: str, prompt: str, recurring: bool) -> str:
            create_calls.append({"cron": cron, "recurring": recurring})
            return "new-job-99"

        handler = HandlerSessionBootstrap(
            cron_list_fn=fake_list, cron_create_fn=fake_create
        )
        with tempfile.TemporaryDirectory() as tmp:
            cmd = ModelBootstrapCommand(
                session_id=str(uuid.uuid4()),
                contract=_VALID_CONTRACT,  # type: ignore[arg-type]
                state_dir=tmp,
                dry_run=False,
                session_mode="build",
            )
            result = handler.handle(cmd)

        assert len(create_calls) == 1
        assert create_calls[0]["cron"] == "*/30 * * * *"
        assert create_calls[0]["recurring"] is True
        assert "new-job-99" in result.crons_registered

    def test_all_crons_fail_produces_failed_status(self) -> None:
        """If all phase-1 crons fail to register, status == FAILED."""

        def fake_list() -> list[dict[str, str]]:
            return []

        def fake_create(cron: str, prompt: str, recurring: bool) -> None:
            return None  # Simulate failure

        handler = HandlerSessionBootstrap(
            cron_list_fn=fake_list, cron_create_fn=fake_create
        )
        with tempfile.TemporaryDirectory() as tmp:
            cmd = ModelBootstrapCommand(
                session_id=str(uuid.uuid4()),
                contract=_VALID_CONTRACT,  # type: ignore[arg-type]
                state_dir=tmp,
                dry_run=False,
                session_mode="build",
            )
            result = handler.handle(cmd)

        assert result.status == EnumBootstrapStatus.FAILED

    def test_cron_ids_written_to_disk(self) -> None:
        """Registered cron IDs are persisted to session-crons-{session_id}.json."""

        def fake_list() -> list[dict[str, str]]:
            return []

        def fake_create(cron: str, prompt: str, recurring: bool) -> str:
            return "job-disk-test"

        session_id = str(uuid.uuid4())
        handler = HandlerSessionBootstrap(
            cron_list_fn=fake_list, cron_create_fn=fake_create
        )
        with tempfile.TemporaryDirectory() as tmp:
            cmd = ModelBootstrapCommand(
                session_id=session_id,
                contract=_VALID_CONTRACT,  # type: ignore[arg-type]
                state_dir=tmp,
                dry_run=False,
                session_mode="build",
            )
            result = handler.handle(cmd)

            cron_file = os.path.join(tmp, f"session-crons-{session_id}.json")
            assert os.path.isfile(cron_file), "session-crons JSON not written"
            with open(cron_file) as f:
                payload = json.load(f)
            assert payload["session_id"] == session_id
            assert "job-disk-test" in payload["cron_ids"]

        assert "job-disk-test" in result.crons_registered

    def test_contract_snapshot_includes_rev7_fields(self) -> None:
        """Rev 7 contract snapshot includes session_mode, active_sprint_id, model_routing_preference."""
        session_id = str(uuid.uuid4())
        handler = HandlerSessionBootstrap()
        with tempfile.TemporaryDirectory() as tmp:
            cmd = ModelBootstrapCommand(
                session_id=session_id,
                contract=_VALID_CONTRACT,  # type: ignore[arg-type]
                state_dir=tmp,
                dry_run=False,
                session_mode="build",
                active_sprint_id="auto-detect",
                model_routing_preference="local-first",
            )
            result = handler.handle(cmd)

            with open(result.contract_path) as f:
                payload = json.load(f)

        assert payload["session_mode"] == "build"
        assert payload["active_sprint_id"] == "auto-detect"
        assert payload["model_routing_preference"] == "local-first"
