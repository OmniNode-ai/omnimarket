# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_session_bootstrap Rev 7.

Covers:
- Original golden chain (dry_run, READY/DEGRADED status, filesystem writes)
- CronList dedup — existing cron skips CronCreate (C5 fix)
- Dispatch lease blocks concurrent acquisition (C4 fix)
- EnumDodCheckType registry covers all values (C6 fix)
- Cross-tick ID verification detects hallucinated PASS (C2 fix)
- VACUOUS_PULSE detected and written to disk (C1 fix)
- Handler writes cron IDs file on success
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import UTC, datetime

import pytest

from omnimarket.nodes.node_session_bootstrap.cron_output_verification import (
    CronOutputVerificationRoutine,
    VerificationInput,
)
from omnimarket.nodes.node_session_bootstrap.dispatch_lease import (
    dispatch_lease,
    release_lease,
    try_acquire_lease,
)
from omnimarket.nodes.node_session_bootstrap.dod_verification_registry import (
    run_dod_check,
)
from omnimarket.nodes.node_session_bootstrap.handlers.handler_session_bootstrap import (
    EnumBootstrapStatus,
    HandlerSessionBootstrap,
    ModelBootstrapCommand,
)
from omnimarket.nodes.node_session_bootstrap.models.model_task_contract import (
    EnumDodCheckType,
    ModelDodEvidenceCheck,
    ModelTaskContract,
)

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
        handler = HandlerSessionBootstrap()
        cmd = _make_command(dry_run=True)
        result = handler.handle(cmd)

        assert result.contract_path == "(dry-run)"
        assert result.dry_run is True

    def test_ready_status_valid_contract_dry_run(self) -> None:
        handler = HandlerSessionBootstrap()
        cmd = _make_command(dry_run=True)
        result = handler.handle(cmd)

        assert result.status == EnumBootstrapStatus.READY
        assert result.warnings == []

    def test_warns_on_empty_phases(self) -> None:
        contract = dict(_VALID_CONTRACT)
        contract["phases_expected"] = []
        handler = HandlerSessionBootstrap()
        cmd = _make_command(contract=contract, dry_run=True)
        result = handler.handle(cmd)

        assert result.status == EnumBootstrapStatus.DEGRADED
        assert any("phases_expected is empty" in w for w in result.warnings)

    def test_warns_on_high_cost_ceiling(self) -> None:
        contract = dict(_VALID_CONTRACT)
        contract["cost_ceiling_usd"] = 50.0
        handler = HandlerSessionBootstrap()
        cmd = _make_command(contract=contract, dry_run=True)
        result = handler.handle(cmd)

        assert any("cost_ceiling_usd" in w for w in result.warnings)

    def test_session_id_round_trips(self) -> None:
        session_id = str(uuid.uuid4())
        handler = HandlerSessionBootstrap()
        cmd = _make_command(session_id=session_id, dry_run=True)
        result = handler.handle(cmd)

        assert result.session_id == session_id

    def test_contract_path_contains_session_id(self) -> None:
        session_id = str(uuid.uuid4())
        handler = HandlerSessionBootstrap()
        with tempfile.TemporaryDirectory() as tmp:
            cmd = _make_command(session_id=session_id, state_dir=tmp, dry_run=False)
            result = handler.handle(cmd)

        assert session_id in result.contract_path
        assert result.contract_path != "(dry-run)"

    def test_contract_file_written_on_disk(self) -> None:
        session_id = str(uuid.uuid4())
        handler = HandlerSessionBootstrap()
        with tempfile.TemporaryDirectory() as tmp:
            cmd = _make_command(session_id=session_id, state_dir=tmp, dry_run=False)
            result = handler.handle(cmd)

            assert os.path.isfile(result.contract_path)
            with open(result.contract_path) as f:
                payload = json.load(f)
            assert payload["session_id"] == session_id
            # v2: contract snapshot includes session_mode and routing pref
            assert "session_mode" in payload
            assert "model_routing_preference" in payload

    def test_event_bus_wiring(self, event_bus: object) -> None:
        handler = HandlerSessionBootstrap()
        cmd = _make_command(dry_run=True)
        result = handler.handle(cmd)

        assert result.status in (EnumBootstrapStatus.READY, EnumBootstrapStatus.DEGRADED)

    def test_result_serializes_to_json(self) -> None:
        handler = HandlerSessionBootstrap()
        cmd = _make_command(dry_run=True)
        result = handler.handle(cmd)

        raw = result.model_dump_json()
        parsed = json.loads(raw)
        assert parsed["session_id"] == cmd.session_id
        assert "bootstrapped_at" in parsed
        assert "crons_registered" in parsed

    # --- Rev 7: CronList dedup ---

    def test_cronlist_dedup_skips_existing_cron(self) -> None:
        create_calls: list[dict[str, object]] = []

        def fake_create(**kwargs: object) -> dict[str, object]:
            create_calls.append(dict(kwargs))
            return {"id": "new-job"}

        def fake_list() -> list[dict[str, object]]:
            return [{"name": "build-dispatch-pulse"}]

        handler = HandlerSessionBootstrap(cron_create_fn=fake_create, cron_list_fn=fake_list)
        with tempfile.TemporaryDirectory() as tmp:
            cmd = _make_command(state_dir=tmp, dry_run=False)
            result = handler.handle(cmd)

        assert len(create_calls) == 0
        assert any("existing:build-dispatch-pulse" in c for c in result.crons_registered)
        assert any("cron already registered" in w for w in result.warnings)

    def test_croncreate_called_when_absent(self) -> None:
        create_calls: list[dict[str, object]] = []

        def fake_create(**kwargs: object) -> dict[str, object]:
            create_calls.append(dict(kwargs))
            return {"id": "job-abc123"}

        def fake_list() -> list[dict[str, object]]:
            return []

        handler = HandlerSessionBootstrap(cron_create_fn=fake_create, cron_list_fn=fake_list)
        with tempfile.TemporaryDirectory() as tmp:
            cmd = _make_command(state_dir=tmp, dry_run=False)
            result = handler.handle(cmd)

        assert len(create_calls) == 1
        assert create_calls[0]["recurring"] is True
        assert "job-abc123" in result.crons_registered
        assert result.status == EnumBootstrapStatus.READY

    def test_handler_writes_cron_ids_file(self) -> None:
        def fake_create(**kwargs: object) -> dict[str, object]:
            return {"id": "cron-xyz"}

        handler = HandlerSessionBootstrap(cron_create_fn=fake_create, cron_list_fn=lambda: [])
        session_id = "test-session-crons"
        with tempfile.TemporaryDirectory() as tmp:
            cmd = _make_command(session_id=session_id, state_dir=tmp, dry_run=False)
            handler.handle(cmd)

            cron_file = os.path.join(tmp, f"session-crons-{session_id}.json")
            assert os.path.exists(cron_file)
            with open(cron_file) as fh:
                data = json.load(fh)
            assert "cron-xyz" in data["crons_registered"]

    def test_all_crons_fail_returns_failed_status(self) -> None:
        def fail_create(**kwargs: object) -> dict[str, object]:
            raise RuntimeError("CronCreate unavailable")

        handler = HandlerSessionBootstrap(cron_create_fn=fail_create, cron_list_fn=lambda: [])
        with tempfile.TemporaryDirectory() as tmp:
            cmd = _make_command(state_dir=tmp, dry_run=False)
            result = handler.handle(cmd)

        assert result.status == EnumBootstrapStatus.FAILED
        assert any("CronCreate failed" in w for w in result.warnings)


@pytest.mark.unit
class TestDispatchLease:
    def test_lease_blocks_second_acquire(self, tmp_path: object) -> None:
        state_dir = str(tmp_path)
        assert try_acquire_lease(state_dir, "tick-1", "pulse") is True
        assert try_acquire_lease(state_dir, "tick-2", "loop") is False
        release_lease(state_dir)
        assert try_acquire_lease(state_dir, "tick-3", "pulse") is True
        release_lease(state_dir)

    def test_context_manager_releases_on_exit(self, tmp_path: object) -> None:
        state_dir = str(tmp_path)
        with dispatch_lease(state_dir, "tick-1", "pulse") as acquired:
            assert acquired is True
            assert os.path.exists(os.path.join(state_dir, "dispatch-lock.json"))
        assert not os.path.exists(os.path.join(state_dir, "dispatch-lock.json"))

    def test_context_manager_yields_false_when_held(self, tmp_path: object) -> None:
        state_dir = str(tmp_path)
        try_acquire_lease(state_dir, "tick-1", "pulse")
        with dispatch_lease(state_dir, "tick-2", "loop") as acquired:
            assert acquired is False
        release_lease(state_dir)


@pytest.mark.unit
class TestDodRegistry:
    def test_registry_covers_all_enum_values(self) -> None:
        contract = ModelTaskContract(
            task_id="build-0001",
            ticket_id="OMN-0001",
            target_repo="OmniNode-ai/omnimarket",
            target_branch_pattern="jonah/omn-0001-*",
            dod_evidence=[],
            dispatched_at=datetime.now(tz=UTC),
            dispatch_path="agent_bypass",
            model_used="sonnet",
        )
        for check_type in EnumDodCheckType:
            check = ModelDodEvidenceCheck(check_type=check_type)
            result = run_dod_check(contract, check)
            assert hasattr(result, "passed")
            assert hasattr(result, "detail")


@pytest.mark.unit
class TestCronOutputVerification:
    def test_vacuous_pulse_detected(self, tmp_path: object) -> None:
        verifier = CronOutputVerificationRoutine(str(tmp_path))
        inputs = VerificationInput(
            tick_id="tick-vacuous",
            dispatched_task_ids=[],
            backlog_unworked_count=5,
            dispatch_path_used="none",
            dogfood_available=True,
            session_id="sess-001",
        )
        result = verifier.verify(inputs)

        assert result.verdict == "fail"
        assert "VACUOUS_PULSE" in result.failure_reason
        tick_file = os.path.join(str(tmp_path), "pulse-ticks", "tick-vacuous.json")
        assert os.path.exists(tick_file)
        with open(tick_file) as fh:
            data = json.load(fh)
        assert data["verdict"] == "fail"
        friction_files = os.listdir(os.path.join(str(tmp_path), "friction"))
        assert any("vacuous-pulse" in f for f in friction_files)

    def test_pass_when_backlog_empty(self, tmp_path: object) -> None:
        verifier = CronOutputVerificationRoutine(str(tmp_path))
        inputs = VerificationInput(
            tick_id="tick-empty",
            dispatched_task_ids=[],
            backlog_unworked_count=0,
            dispatch_path_used="none",
            dogfood_available=True,
            session_id="sess-001",
        )
        result = verifier.verify(inputs)
        assert result.verdict == "pass"

    def test_cross_tick_detects_hallucinated_pass(self, tmp_path: object) -> None:
        state_dir = str(tmp_path)
        verifier = CronOutputVerificationRoutine(state_dir)

        ticks_dir = os.path.join(state_dir, "pulse-ticks")
        os.makedirs(ticks_dir)
        prev_path = os.path.join(ticks_dir, "tick-prev.json")
        with open(prev_path, "w") as fh:
            json.dump({
                "tick_id": "tick-prev",
                "dispatched_count": 1,
                "dispatched_task_ids": ["build-9999"],
                "backlog_unworked_count": 0,
                "dispatch_path_used": "dogfood",
                "verdict": "pass",
            }, fh)

        # No matching dispatch-event file exists — hallucinated PASS
        inputs = VerificationInput(
            tick_id="tick-current",
            dispatched_task_ids=[],
            backlog_unworked_count=0,
            dispatch_path_used="none",
            dogfood_available=True,
            session_id="sess-001",
            previous_tick_result_path=prev_path,
        )
        verifier.verify(inputs)
        assert any("hallucinated_pass" in w for w in inputs.warnings)

    def test_find_latest_tick_returns_none_when_empty(self, tmp_path: object) -> None:
        verifier = CronOutputVerificationRoutine(str(tmp_path))
        assert verifier.find_latest_tick_result_path() is None
