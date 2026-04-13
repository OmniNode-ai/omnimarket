# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_session_bootstrap (Rev 7, Phase 2).

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
    _REQUIRED_CRONS,
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

# Cron names active in each mode — derived from _REQUIRED_CRONS for single source of truth
_CRONS_FOR_MODE: dict[str, list[str]] = {
    mode: [s.cron_name for s in _REQUIRED_CRONS if mode in s.active_modes]
    for mode in ("build", "close-out", "reporting")
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
    # Rev 7 / Phase 1 tests (preserved)
    # ------------------------------------------------------------------

    def test_dry_run_build_mode_records_cron_placeholder(self) -> None:
        """dry_run=True in build mode -> crons_registered contains dry-run placeholder."""
        handler = HandlerSessionBootstrap()
        cmd = _make_command(dry_run=True, session_mode="build")
        result = handler.handle(cmd)

        assert any("dry-run" in c for c in result.crons_registered)

    def test_invalid_session_mode_degrades(self) -> None:
        """Unknown session_mode -> DEGRADED with warning."""
        handler = HandlerSessionBootstrap()
        cmd = _make_command(dry_run=True, session_mode="unknown-mode")
        result = handler.handle(cmd)

        assert result.status == EnumBootstrapStatus.DEGRADED
        assert any("session_mode" in w for w in result.warnings)

    def test_cron_skip_when_already_registered(self) -> None:
        """C5: If build-dispatch-pulse already in CronList, CronCreate NOT called for it; ID recorded."""
        # All 4 crons are pre-registered so create should never be called
        existing = [
            {"name": "build-dispatch-pulse", "id": "existing-pulse-42"},
            {"name": "merge-sweep", "id": "existing-merge-43"},
            {"name": "overseer-verify", "id": "existing-overseer-44"},
            {"name": "contract-verify", "id": "existing-contract-45"},
        ]
        create_calls: list[dict[str, object]] = []

        def fake_list() -> list[dict[str, str]]:
            return existing  # type: ignore[return-value]

        def fake_create(**kwargs: object) -> str:
            create_calls.append(dict(kwargs))
            return "new-job-id"

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

        assert create_calls == [], (
            "CronCreate must NOT be called when cron already exists"
        )
        assert "existing-pulse-42" in result.crons_registered

    def test_all_crons_fail_produces_failed_status(self) -> None:
        """If phase-1 cron (build-dispatch-pulse) fails to register, status == FAILED."""

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

    # ------------------------------------------------------------------
    # Phase 2 tests — all 4 standard crons
    # ------------------------------------------------------------------

    def test_all_four_crons_created_when_list_empty(self) -> None:
        """Phase 2: when CronList returns empty, all 4 crons are created in build mode."""
        create_calls: list[dict[str, object]] = []

        def fake_list() -> list[dict[str, str]]:
            return []

        def fake_create(cron: str, prompt: str, recurring: bool) -> str:
            create_calls.append({"cron": cron, "recurring": recurring})
            return f"job-{len(create_calls)}"

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

        expected_count = len(_CRONS_FOR_MODE["build"])
        assert len(create_calls) == expected_count, (
            f"Expected {expected_count} CronCreate calls for build mode, got {len(create_calls)}"
        )
        assert len(result.crons_registered) == expected_count
        # build-dispatch-pulse must always be among them
        assert any("*/30" in str(c["cron"]) for c in create_calls), (
            "build-dispatch-pulse (*/30 * * * *) must be created"
        )

    def test_idempotent_partial_state_only_missing_crons_created(self) -> None:
        """Phase 2: when 2 of 4 crons already exist, only the 2 missing are created."""
        existing = [
            {"name": "build-dispatch-pulse", "id": "existing-pulse"},
            {"name": "merge-sweep", "id": "existing-merge"},
        ]
        create_calls: list[str] = []

        def fake_list() -> list[dict[str, str]]:
            return existing  # type: ignore[return-value]

        def fake_create(cron: str, prompt: str, recurring: bool) -> str:
            create_calls.append(cron)
            return f"new-{len(create_calls)}"

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

        # 2 existing + 2 created = 4 total in build mode
        assert len(create_calls) == 2, (
            f"Expected 2 CronCreate calls (missing crons only), got {len(create_calls)}"
        )
        assert len(result.crons_registered) == len(_CRONS_FOR_MODE["build"])
        assert "existing-pulse" in result.crons_registered
        assert "existing-merge" in result.crons_registered

    def test_reporting_mode_activates_overseer_and_contract_verify(self) -> None:
        """Phase 2: reporting mode activates overseer-verify and contract-verify crons."""
        create_calls: list[str] = []

        def fake_list() -> list[dict[str, str]]:
            return []

        def fake_create(cron: str, prompt: str, recurring: bool) -> str:
            create_calls.append(cron)
            return f"job-{len(create_calls)}"

        handler = HandlerSessionBootstrap(
            cron_list_fn=fake_list, cron_create_fn=fake_create
        )
        with tempfile.TemporaryDirectory() as tmp:
            cmd = ModelBootstrapCommand(
                session_id=str(uuid.uuid4()),
                contract=_VALID_CONTRACT,  # type: ignore[arg-type]
                state_dir=tmp,
                dry_run=False,
                session_mode="reporting",
            )
            result = handler.handle(cmd)

        expected = _CRONS_FOR_MODE["reporting"]
        assert len(create_calls) == len(expected), (
            f"Expected {len(expected)} crons for reporting mode, got {len(create_calls)}"
        )
        assert len(result.crons_registered) == len(expected)
        # build-dispatch-pulse must NOT be in reporting mode
        assert (
            "build-dispatch-pulse"
            not in [
                s.cron_name for s in _REQUIRED_CRONS if "reporting" in s.active_modes
            ]
            or True
        )  # assert via _CRONS_FOR_MODE membership
        assert "build-dispatch-pulse" not in _CRONS_FOR_MODE["reporting"]

    def test_dry_run_reporting_mode_records_placeholders_for_active_crons(self) -> None:
        """Phase 2: dry_run reporting mode -> placeholders for overseer + contract crons."""
        handler = HandlerSessionBootstrap()
        cmd = _make_command(dry_run=True, session_mode="reporting")
        result = handler.handle(cmd)

        expected_count = len(_CRONS_FOR_MODE["reporting"])
        assert len(result.crons_registered) == expected_count, (
            f"Expected {expected_count} dry-run placeholders for reporting mode"
        )
        assert all("dry-run" in c for c in result.crons_registered)

    def test_build_dispatch_pulse_cron_expression_is_correct(self) -> None:
        """Phase 2: build-dispatch-pulse uses */30 * * * * expression."""
        create_calls: list[dict[str, object]] = []

        def fake_list() -> list[dict[str, str]]:
            return []

        def fake_create(cron: str, prompt: str, recurring: bool) -> str:
            create_calls.append({"cron": cron, "name_hint": prompt[:50]})
            return f"job-{len(create_calls)}"

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
            handler.handle(cmd)

        pulse_calls = [c for c in create_calls if c["cron"] == "*/30 * * * *"]
        assert len(pulse_calls) == 1, "build-dispatch-pulse must use */30 * * * *"

    def test_merge_sweep_cron_expression_is_correct(self) -> None:
        """Phase 2: merge-sweep uses 23 * * * * expression."""
        create_calls: list[dict[str, object]] = []

        def fake_list() -> list[dict[str, str]]:
            return []

        def fake_create(cron: str, prompt: str, recurring: bool) -> str:
            create_calls.append({"cron": cron, "prompt_head": prompt[:80]})
            return f"job-{len(create_calls)}"

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
            handler.handle(cmd)

        merge_calls = [c for c in create_calls if c["cron"] == "23 * * * *"]
        assert len(merge_calls) == 1, "merge-sweep must use 23 * * * *"

    def test_overseer_verify_cron_expression_is_correct(self) -> None:
        """Phase 2: overseer-verify uses 11,31,51 * * * * expression."""
        create_calls: list[dict[str, object]] = []

        def fake_list() -> list[dict[str, str]]:
            return []

        def fake_create(cron: str, prompt: str, recurring: bool) -> str:
            create_calls.append({"cron": cron})
            return f"job-{len(create_calls)}"

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
            handler.handle(cmd)

        overseer_calls = [c for c in create_calls if c["cron"] == "11,31,51 * * * *"]
        assert len(overseer_calls) == 1, "overseer-verify must use 11,31,51 * * * *"

    def test_contract_verify_cron_expression_is_correct(self) -> None:
        """Phase 2: contract-verify uses 7,22,37,52 * * * * expression."""
        create_calls: list[dict[str, object]] = []

        def fake_list() -> list[dict[str, str]]:
            return []

        def fake_create(cron: str, prompt: str, recurring: bool) -> str:
            create_calls.append({"cron": cron})
            return f"job-{len(create_calls)}"

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
            handler.handle(cmd)

        contract_calls = [c for c in create_calls if c["cron"] == "7,22,37,52 * * * *"]
        assert len(contract_calls) == 1, "contract-verify must use 7,22,37,52 * * * *"

    def test_phase2_cron_failure_does_not_produce_failed_status(self) -> None:
        """Phase 2: if only phase-2 crons fail (pulse succeeds), status is DEGRADED not FAILED."""
        pulse_cron = "*/30 * * * *"

        def fake_list() -> list[dict[str, str]]:
            return []

        def fake_create(cron: str, prompt: str, recurring: bool) -> str | None:
            # Only succeed for build-dispatch-pulse
            if cron == pulse_cron:
                return "pulse-job-ok"
            return None  # fail phase-2 crons

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

        assert result.status == EnumBootstrapStatus.DEGRADED, (
            "Phase-2 cron failures should degrade, not fail"
        )
        assert "pulse-job-ok" in result.crons_registered
