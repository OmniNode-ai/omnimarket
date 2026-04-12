# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_session_bootstrap (Rev 7).

Verifies: bootstrap command -> handler -> ModelBootstrapResult.
Rev 7 additions: CronList pre-check, dispatch lease, EnumDodCheckType.
Uses EventBusInmemory. No subprocess calls. No real filesystem writes (dry_run).
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_session_bootstrap.handlers.handler_session_bootstrap import (
    EnumBootstrapStatus,
    HandlerSessionBootstrap,
    ModelBootstrapCommand,
)
from omnimarket.nodes.node_session_bootstrap.models.models_task_contract import (
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


def _make_cron_entry(name: str) -> MagicMock:
    m = MagicMock()
    m.name = name
    return m


def _make_cron_create_result(job_id: str) -> MagicMock:
    m = MagicMock()
    m.job_id = job_id
    return m


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
        """Valid contract with phases and dry_run -> status == READY."""
        handler = HandlerSessionBootstrap()
        cmd = _make_command(dry_run=True)
        result = handler.handle(cmd)

        assert result.status == EnumBootstrapStatus.READY

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


@pytest.mark.unit
class TestCronListIdempotency:
    """Rev 7 C5: CronList pre-check prevents duplicate CronCreate."""

    def test_cron_already_registered_skips_create(self) -> None:
        """If CronList returns matching cron, CronCreate is NOT called."""
        cron_list = MagicMock(return_value=[_make_cron_entry("build-dispatch-pulse")])
        cron_create = MagicMock()

        handler = HandlerSessionBootstrap(cron_list=cron_list, cron_create=cron_create)
        cmd = _make_command(dry_run=False)
        with tempfile.TemporaryDirectory() as tmp:
            cmd = _make_command(dry_run=False, state_dir=tmp)
            result = handler.handle(cmd)

        cron_list.assert_called_once()
        cron_create.assert_not_called()
        assert any("already registered" in w for w in result.warnings)
        assert len(result.crons_registered) == 1

    def test_new_cron_calls_create(self) -> None:
        """If CronList returns no matching cron, CronCreate IS called."""
        cron_list = MagicMock(return_value=[])
        job_id = "job-abc123"
        cron_create = MagicMock(return_value=_make_cron_create_result(job_id))

        handler = HandlerSessionBootstrap(cron_list=cron_list, cron_create=cron_create)
        with tempfile.TemporaryDirectory() as tmp:
            cmd = _make_command(dry_run=False, state_dir=tmp)
            result = handler.handle(cmd)

        cron_create.assert_called_once()
        assert job_id in result.crons_registered

    def test_cron_list_failure_degrades_gracefully(self) -> None:
        """CronList failure -> warning added, status degraded, no CronCreate called."""
        cron_list = MagicMock(side_effect=RuntimeError("SDK unavailable"))
        cron_create = MagicMock()

        handler = HandlerSessionBootstrap(cron_list=cron_list, cron_create=cron_create)
        cmd = _make_command(dry_run=False)
        result = handler.handle(cmd)

        cron_create.assert_not_called()
        assert any("CronList failed" in w for w in result.warnings)

    def test_no_cron_tools_injected_warns(self) -> None:
        """No cron tools injected -> warning about skipped registration."""
        handler = HandlerSessionBootstrap()
        cmd = _make_command(dry_run=False)
        result = handler.handle(cmd)

        assert any("not injected" in w for w in result.warnings)

    def test_dry_run_returns_sentinel_job_id(self) -> None:
        """dry_run=True -> crons_registered contains sentinel without calling tools."""
        cron_list = MagicMock()
        cron_create = MagicMock()

        handler = HandlerSessionBootstrap(cron_list=cron_list, cron_create=cron_create)
        cmd = _make_command(dry_run=True)
        result = handler.handle(cmd)

        cron_list.assert_not_called()
        cron_create.assert_not_called()
        assert "dry-run-job-id" in result.crons_registered

    def test_non_build_mode_skips_cron_registration(self) -> None:
        """session_mode=reporting -> no cron registration attempted."""
        cron_list = MagicMock()
        cron_create = MagicMock()

        handler = HandlerSessionBootstrap(cron_list=cron_list, cron_create=cron_create)
        cmd = _make_command(dry_run=True, session_mode="reporting")
        result = handler.handle(cmd)

        cron_list.assert_not_called()
        cron_create.assert_not_called()
        assert result.crons_registered == []

    def test_cron_ids_written_to_disk(self) -> None:
        """When crons are registered, session-crons-{session_id}.json is written."""
        job_id = "job-written-to-disk"
        cron_list = MagicMock(return_value=[])
        cron_create = MagicMock(return_value=_make_cron_create_result(job_id))

        handler = HandlerSessionBootstrap(cron_list=cron_list, cron_create=cron_create)
        session_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as tmp:
            cmd = _make_command(session_id=session_id, dry_run=False, state_dir=tmp)
            handler.handle(cmd)
            cron_file = os.path.join(tmp, f"session-crons-{session_id}.json")
            assert os.path.isfile(cron_file)
            with open(cron_file) as f:
                data = json.load(f)
            assert job_id in data["cron_job_ids"]


@pytest.mark.unit
class TestDispatchLease:
    """Rev 7 C4: File-based dispatch lease."""

    def test_acquire_creates_lock_file(self) -> None:
        handler = HandlerSessionBootstrap()
        with tempfile.TemporaryDirectory() as tmp:
            acquired = handler.acquire_dispatch_lease(tmp, "tick-001", "test")
            assert acquired is True
            lock_path = os.path.join(tmp, "dispatch-lock.json")
            assert os.path.isfile(lock_path)
            with open(lock_path) as f:
                data = json.load(f)
            assert data["tick_id"] == "tick-001"
            assert data["holder"] == "test"

    def test_acquire_blocked_by_fresh_lease(self) -> None:
        """Fresh lease held by another -> acquire returns False."""
        handler = HandlerSessionBootstrap()
        with tempfile.TemporaryDirectory() as tmp:
            handler.acquire_dispatch_lease(tmp, "tick-001", "holder-a")
            acquired = handler.acquire_dispatch_lease(tmp, "tick-002", "holder-b")
            assert acquired is False

    def test_acquire_overwrites_stale_lease(self) -> None:
        """Lease older than 30 min -> overwritten by new acquire."""
        from datetime import timedelta

        handler = HandlerSessionBootstrap()
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = os.path.join(tmp, "dispatch-lock.json")
            stale_time = (
                __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                - timedelta(seconds=2000)
            ).isoformat()
            with open(lock_path, "w") as f:
                json.dump(
                    {"tick_id": "old-tick", "acquired_at": stale_time, "holder": "old"},
                    f,
                )
            acquired = handler.acquire_dispatch_lease(tmp, "tick-new", "new-holder")
            assert acquired is True
            with open(lock_path) as f:
                data = json.load(f)
            assert data["holder"] == "new-holder"

    def test_release_deletes_lock_file(self) -> None:
        handler = HandlerSessionBootstrap()
        with tempfile.TemporaryDirectory() as tmp:
            handler.acquire_dispatch_lease(tmp, "tick-001", "test")
            handler.release_dispatch_lease(tmp)
            lock_path = os.path.join(tmp, "dispatch-lock.json")
            assert not os.path.exists(lock_path)

    def test_release_no_lock_file_is_safe(self) -> None:
        """release_dispatch_lease on non-existent file logs warning, does not raise."""
        handler = HandlerSessionBootstrap()
        with tempfile.TemporaryDirectory() as tmp:
            handler.release_dispatch_lease(tmp)  # should not raise


@pytest.mark.unit
class TestEnumDodCheckType:
    """Rev 7 C6: EnumDodCheckType — closed enum, no arbitrary commands."""

    def test_all_expected_values_present(self) -> None:
        expected = {
            "pr_opened",
            "tests_pass",
            "golden_chain",
            "pre_commit_clean",
            "rendered_output",
            "overseer_5check",
        }
        actual = {v.value for v in EnumDodCheckType}
        assert actual == expected

    def test_model_dod_evidence_check_construction(self) -> None:
        check = ModelDodEvidenceCheck(check_type=EnumDodCheckType.PR_OPENED)
        assert check.check_type == EnumDodCheckType.PR_OPENED
        assert check.required is True
        assert check.timeout_seconds == 30

    def test_model_task_contract_construction(self) -> None:
        import datetime

        contract = ModelTaskContract(
            task_id="build-8505",
            ticket_id="OMN-8505",
            target_repo="OmniNode-ai/omnimarket",
            target_branch_pattern="jonah/omn-8505-*",
            dod_evidence=[
                ModelDodEvidenceCheck(check_type=EnumDodCheckType.PR_OPENED),
                ModelDodEvidenceCheck(check_type=EnumDodCheckType.PRE_COMMIT_CLEAN),
            ],
            dispatched_at=datetime.datetime.now(datetime.UTC),
            dispatch_path="dogfood",
            model_used="qwen3-coder-30b",
        )
        assert contract.task_id == "build-8505"
        assert len(contract.dod_evidence) == 2

    def test_unknown_check_type_raises(self) -> None:
        with pytest.raises(ValueError, match="rm -rf"):
            ModelDodEvidenceCheck(check_type="rm -rf /")  # type: ignore[arg-type]

    def test_dod_registry_run_check_unknown_returns_false(self) -> None:
        import datetime

        from omnimarket.nodes.node_session_bootstrap.handlers.dod_verification_registry import (
            run_check,
        )

        contract = ModelTaskContract(
            task_id="t1",
            ticket_id="OMN-1",
            target_repo="OmniNode-ai/omnimarket",
            target_branch_pattern="jonah/omn-1-*",
            dod_evidence=[],
            dispatched_at=datetime.datetime.now(datetime.UTC),
            dispatch_path="dogfood",
            model_used="local",
        )
        result = run_check(contract, "nonexistent_check_type")
        assert result is False
