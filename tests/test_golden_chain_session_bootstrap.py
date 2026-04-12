# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_session_bootstrap (Rev 7).

Tests cover: basic golden chain, CronList idempotency (C5), dispatch lease (C4),
EnumDodCheckType registry safety (C6), severity-ranked status accumulator.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_session_bootstrap.handlers.handler_session_bootstrap import (
    CronEntry,
    EnumBootstrapStatus,
    HandlerSessionBootstrap,
    ModelBootstrapCommand,
    NullCronScheduler,
    acquire_dispatch_lease,
    release_dispatch_lease,
)
from omnimarket.nodes.node_session_bootstrap.models.dod_verification_registry import (
    _REGISTRY,
    run_check,
)
from omnimarket.nodes.node_session_bootstrap.models.model_session_contract import (
    ModelSessionContract,
)
from omnimarket.nodes.node_session_bootstrap.models.models_task_contract import (
    EnumDodCheckType,
    ModelDodEvidenceCheck,
    ModelTaskContract,
)

CMD_TOPIC = "onex.cmd.omnimarket.session-bootstrap-start.v2"
EVT_TOPIC = "onex.evt.omnimarket.session-bootstrap-completed.v2"

_VALID_CONTRACT: dict[str, object] = {
    "session_id": "test-session-001",
    "session_label": "2026-04-12 overnight",
    "phases_expected": ["build_loop", "merge_sweep", "platform_readiness"],
    "max_cycles": 0,
    "cost_ceiling_usd": 10.0,
    "halt_on_build_loop_failure": True,
    "dry_run": False,
    "schema_version": "1.0",
    "session_mode": "build",
    "active_sprint_id": "auto-detect",
    "model_routing_preference": "local-first",
}


def _make_contract(**overrides: object) -> ModelSessionContract:
    data = dict(_VALID_CONTRACT, **overrides)
    return ModelSessionContract(**data)  # type: ignore[arg-type]


def _make_command(
    session_id: str | None = None,
    contract: ModelSessionContract | None = None,
    state_dir: str = ".onex_state",
    dry_run: bool = True,
) -> ModelBootstrapCommand:
    return ModelBootstrapCommand(
        session_id=session_id or str(uuid.uuid4()),
        contract=contract or _make_contract(),
        state_dir=state_dir,
        dry_run=dry_run,
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

    def test_ready_status_valid_contract(self) -> None:
        handler = HandlerSessionBootstrap()
        cmd = _make_command(dry_run=True)
        result = handler.handle(cmd)

        assert result.status == EnumBootstrapStatus.READY
        assert result.warnings == []

    def test_warns_on_empty_phases(self) -> None:
        contract = _make_contract(phases_expected=[])
        handler = HandlerSessionBootstrap()
        cmd = _make_command(contract=contract, dry_run=True)
        result = handler.handle(cmd)

        assert result.status == EnumBootstrapStatus.DEGRADED
        assert any("phases_expected is empty" in w for w in result.warnings)

    def test_warns_on_high_cost_ceiling(self) -> None:
        contract = _make_contract(cost_ceiling_usd=50.0)
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
        handler = HandlerSessionBootstrap(cron_scheduler=NullCronScheduler())
        with tempfile.TemporaryDirectory() as tmp:
            cmd = _make_command(session_id=session_id, state_dir=tmp, dry_run=False)
            result = handler.handle(cmd)

        assert session_id in result.contract_path
        assert result.contract_path != "(dry-run)"

    def test_contract_file_written_on_disk(self) -> None:
        session_id = str(uuid.uuid4())
        handler = HandlerSessionBootstrap(cron_scheduler=NullCronScheduler())
        with tempfile.TemporaryDirectory() as tmp:
            cmd = _make_command(session_id=session_id, state_dir=tmp, dry_run=False)
            result = handler.handle(cmd)

            assert os.path.isfile(result.contract_path)
            with open(result.contract_path) as f:
                payload = json.load(f)
            assert payload["session_id"] == session_id

    def test_event_bus_wiring(self, event_bus: object) -> None:
        handler = HandlerSessionBootstrap()
        cmd = _make_command(dry_run=True)
        result = handler.handle(cmd)

        assert result.status in (
            EnumBootstrapStatus.READY,
            EnumBootstrapStatus.DEGRADED,
        )

    def test_result_serializes_to_json(self) -> None:
        handler = HandlerSessionBootstrap()
        cmd = _make_command(dry_run=True)
        result = handler.handle(cmd)

        raw = result.model_dump_json()
        parsed = json.loads(raw)
        assert parsed["session_id"] == cmd.session_id
        assert "bootstrapped_at" in parsed


@pytest.mark.unit
class TestCronIdempotency:
    """Rev 7 C5: CronList pre-check prevents duplicate CronCreate calls."""

    def test_skips_create_when_cron_already_registered(self) -> None:
        scheduler = MagicMock()
        scheduler.list_crons.return_value = [
            CronEntry(job_id="existing-job-123", name="build-dispatch-pulse")
        ]
        handler = HandlerSessionBootstrap(cron_scheduler=scheduler)
        with tempfile.TemporaryDirectory() as tmp:
            cmd = _make_command(state_dir=tmp, dry_run=False)
            result = handler.handle(cmd)

        scheduler.create_cron.assert_not_called()
        assert "existing-job-123" in result.crons_registered

    def test_creates_cron_when_not_registered(self) -> None:
        scheduler = MagicMock()
        scheduler.list_crons.return_value = []
        scheduler.create_cron.return_value = "new-job-456"
        handler = HandlerSessionBootstrap(cron_scheduler=scheduler)
        with tempfile.TemporaryDirectory() as tmp:
            cmd = _make_command(state_dir=tmp, dry_run=False)
            result = handler.handle(cmd)

        scheduler.create_cron.assert_called_once()
        assert "new-job-456" in result.crons_registered

    def test_cron_ids_written_to_disk(self) -> None:
        scheduler = MagicMock()
        scheduler.list_crons.return_value = []
        scheduler.create_cron.return_value = "job-789"
        handler = HandlerSessionBootstrap(cron_scheduler=scheduler)
        session_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as tmp:
            cmd = _make_command(session_id=session_id, state_dir=tmp, dry_run=False)
            handler.handle(cmd)

            cron_file = os.path.join(tmp, f"session-crons-{session_id}.json")
            assert os.path.isfile(cron_file)
            with open(cron_file) as f:
                data = json.load(f)
            assert "job-789" in data["cron_job_ids"]

    def test_phase2_crons_not_activated_in_build_mode(self) -> None:
        scheduler = MagicMock()
        scheduler.list_crons.return_value = []
        scheduler.create_cron.return_value = "job-001"
        handler = HandlerSessionBootstrap(cron_scheduler=scheduler)
        with tempfile.TemporaryDirectory() as tmp:
            cmd = _make_command(state_dir=tmp, dry_run=False)
            handler.handle(cmd)

        calls = [call.kwargs["name"] for call in scheduler.create_cron.call_args_list]
        assert "merge-sweep" not in calls
        assert "overseer-verify" not in calls

    def test_cron_not_created_when_session_mode_not_in_active_modes(self) -> None:
        scheduler = MagicMock()
        scheduler.list_crons.return_value = []
        handler = HandlerSessionBootstrap(cron_scheduler=scheduler)
        contract = _make_contract(session_mode="reporting")
        with tempfile.TemporaryDirectory() as tmp:
            cmd = _make_command(contract=contract, state_dir=tmp, dry_run=False)
            handler.handle(cmd)

        scheduler.create_cron.assert_not_called()


@pytest.mark.unit
class TestDispatchLease:
    """Rev 7 C4: File-based dispatch lease prevents concurrent dispatch."""

    def test_acquire_creates_lease_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            acquired = acquire_dispatch_lease(tmp, holder="test-holder", tick_id="tick-001")
            assert acquired is True
            assert os.path.isfile(os.path.join(tmp, "dispatch-lock.json"))

    def test_second_acquire_returns_false_while_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            acquire_dispatch_lease(tmp, holder="holder-a", tick_id="tick-001")
            acquired = acquire_dispatch_lease(tmp, holder="holder-b", tick_id="tick-002")
            assert acquired is False

    def test_release_removes_lease_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            acquire_dispatch_lease(tmp, holder="holder-a", tick_id="tick-001")
            release_dispatch_lease(tmp)
            assert not os.path.isfile(os.path.join(tmp, "dispatch-lock.json"))

    def test_acquire_after_release_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            acquire_dispatch_lease(tmp, holder="holder-a", tick_id="tick-001")
            release_dispatch_lease(tmp)
            acquired = acquire_dispatch_lease(tmp, holder="holder-b", tick_id="tick-002")
            assert acquired is True

    def test_stale_lease_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lease_path = os.path.join(tmp, "dispatch-lock.json")
            stale = {
                "tick_id": "tick-old",
                "acquired_at": (datetime.now(UTC) - timedelta(minutes=45)).isoformat(),
                "holder": "old-holder",
            }
            with open(lease_path, "w") as f:
                json.dump(stale, f)

            acquired = acquire_dispatch_lease(tmp, holder="new-holder", tick_id="tick-new")
            assert acquired is True
            with open(lease_path) as f:
                data = json.load(f)
            assert data["holder"] == "new-holder"

    def test_release_noop_when_no_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release_dispatch_lease(tmp)  # must not raise


@pytest.mark.unit
class TestEnumDodCheckType:
    """Rev 7 C6: EnumDodCheckType registry — no arbitrary shell commands."""

    def test_all_enum_values_have_registry_entry(self) -> None:
        for member in EnumDodCheckType:
            assert member in _REGISTRY, f"Missing registry entry for {member}"

    def test_unknown_check_type_not_in_registry(self) -> None:
        assert "rm -rf /" not in _REGISTRY
        assert "$(whoami)" not in _REGISTRY

    def test_check_type_is_str_enum(self) -> None:
        for member in EnumDodCheckType:
            assert isinstance(member, str)

    def test_run_check_returns_result(self) -> None:
        check = ModelDodEvidenceCheck(
            check_type=EnumDodCheckType.GOLDEN_CHAIN,
            required=True,
        )
        contract = ModelTaskContract(
            task_id="build-9001",
            ticket_id="OMN-9001",
            target_repo="OmniNode-ai/omnimarket",
            target_branch_pattern="jonah/omn-9001-*",
            dod_evidence=[check],
            dispatched_at=datetime.now(UTC),
            dispatch_path="dogfood",
            model_used="qwen3-coder-30b",
        )
        result = run_check(check, contract)
        assert result.passed is True

    def test_run_check_raises_for_unknown_type(self) -> None:
        check = MagicMock()
        check.check_type = "UNKNOWN_INJECTED_VALUE"
        contract = MagicMock()
        with pytest.raises(ValueError):
            run_check(check, contract)


@pytest.mark.unit
class TestStatusAccumulator:
    """Rev 7: Severity-ranked status accumulator (failed > degraded > ready)."""

    def test_failed_overrides_degraded(self) -> None:
        scheduler = MagicMock()
        scheduler.list_crons.side_effect = Exception("CronList exploded")
        scheduler.create_cron.side_effect = Exception("CronCreate exploded")
        handler = HandlerSessionBootstrap(cron_scheduler=scheduler)
        contract = _make_contract(phases_expected=[], cost_ceiling_usd=99.0)
        with tempfile.TemporaryDirectory() as tmp:
            cmd = _make_command(contract=contract, state_dir=tmp, dry_run=False)
            result = handler.handle(cmd)

        assert result.status == EnumBootstrapStatus.FAILED

    def test_degraded_does_not_regress_to_ready(self) -> None:
        scheduler = MagicMock()
        scheduler.list_crons.return_value = []
        scheduler.create_cron.return_value = "job-ok"
        contract = _make_contract(phases_expected=[])
        handler = HandlerSessionBootstrap(cron_scheduler=scheduler)
        with tempfile.TemporaryDirectory() as tmp:
            cmd = _make_command(contract=contract, state_dir=tmp, dry_run=False)
            result = handler.handle(cmd)

        assert result.status == EnumBootstrapStatus.DEGRADED
