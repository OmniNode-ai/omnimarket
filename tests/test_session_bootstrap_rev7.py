# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for session bootstrap Rev 7 additions.

Covers:
- EnumDodCheckType registry (C6 fix: no arbitrary commands)
- dispatch_lease.py (C4 fix: file-based mutex)
- Cross-tick ID verification data structures
- build_pulse_prompt output content
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.nodes.node_session_bootstrap.dispatch_lease import (
    LEASE_EXPIRY_SECONDS,
    DispatchLeaseHeld,
    acquire_dispatch_lease,
    dispatch_lease,
    lease_age,
    make_tick_id,
    read_dispatch_lease,
    release_dispatch_lease,
)
from omnimarket.nodes.node_session_bootstrap.dod_verification_registry import (
    DOD_VERIFICATION_REGISTRY,
    run_dod_check,
)
from omnimarket.nodes.node_session_bootstrap.handlers.handler_session_bootstrap import (
    build_pulse_prompt,
)
from omnimarket.nodes.node_session_bootstrap.models.model_task_contract import (
    EnumDodCheckType,
    ModelDodEvidenceCheck,
    ModelTaskContract,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_state(tmp_path: object) -> str:
    """Temporary .onex_state directory."""
    assert isinstance(tmp_path, type(tmp_path))  # just use tmp_path type
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        yield d  # type: ignore[misc]


@pytest.fixture
def sample_contract() -> ModelTaskContract:
    return ModelTaskContract(
        task_id="build-8568",
        ticket_id="OMN-8568",
        target_repo="OmniNode-ai/omnimarket",
        target_branch_pattern="jonah/omn-8568-*",
        dod_evidence=[ModelDodEvidenceCheck(check_type=EnumDodCheckType.PR_OPENED)],
        dispatched_at=datetime.now(tz=UTC),
        dispatch_path="agent_bypass",
        model_used="sonnet",
    )


# ---------------------------------------------------------------------------
# EnumDodCheckType — C6 fix
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEnumDodCheckType:
    """EnumDodCheckType is a closed enum: no arbitrary commands."""

    def test_all_values_present_in_registry(self) -> None:
        """Every EnumDodCheckType value must have a registry entry."""
        for check_type in EnumDodCheckType:
            assert check_type in DOD_VERIFICATION_REGISTRY, (
                f"{check_type!r} missing from DOD_VERIFICATION_REGISTRY"
            )

    def test_registry_rejects_unknown_string(self) -> None:
        """run_dod_check with a value not in the registry returns failure."""
        # EnumDodCheckType("rm -rf /") must raise — not a valid enum value
        with pytest.raises((ValueError, KeyError)):
            EnumDodCheckType("rm -rf /")

    def test_enum_values_are_safe_strings(self) -> None:
        """All enum values are simple identifiers — no shell metacharacters."""
        import re

        # Allow lowercase letters, digits, and underscores (e.g. overseer_5check)
        safe = re.compile(r"^[a-z0-9_]+$")
        for check_type in EnumDodCheckType:
            assert safe.match(check_type.value), (
                f"Unsafe enum value: {check_type.value!r}"
            )

    def test_model_dod_evidence_check_roundtrip(self) -> None:
        """ModelDodEvidenceCheck serializes and deserializes correctly."""
        check = ModelDodEvidenceCheck(
            check_type=EnumDodCheckType.PR_OPENED,
            required=True,
            timeout_seconds=30,
        )
        data = check.model_dump()
        assert data["check_type"] == "pr_opened"
        restored = ModelDodEvidenceCheck.model_validate(data)
        assert restored.check_type == EnumDodCheckType.PR_OPENED

    def test_model_task_contract_roundtrip(
        self, sample_contract: ModelTaskContract
    ) -> None:
        """ModelTaskContract serializes to JSON and back cleanly."""
        data = sample_contract.model_dump_json()
        parsed = ModelTaskContract.model_validate_json(data)
        assert parsed.task_id == sample_contract.task_id
        assert parsed.ticket_id == sample_contract.ticket_id
        assert parsed.dod_evidence[0].check_type == EnumDodCheckType.PR_OPENED

    def test_stall_timeout_seconds_defaults_none(
        self, sample_contract: ModelTaskContract
    ) -> None:
        """stall_timeout_seconds defaults to None (no override)."""
        assert sample_contract.stall_timeout_seconds is None

    def test_run_dod_check_pr_opened_without_gh(
        self, sample_contract: ModelTaskContract
    ) -> None:
        """run_dod_check(PR_OPENED) fails gracefully when gh is not configured."""
        # In test environment, gh may fail with non-zero exit or timeout.
        # We just verify the function returns (bool, str) without raising.
        passed, detail = run_dod_check(sample_contract, EnumDodCheckType.PR_OPENED)
        assert isinstance(passed, bool)
        assert isinstance(detail, str)

    def test_run_dod_check_rendered_output_always_passes(
        self, sample_contract: ModelTaskContract
    ) -> None:
        """rendered_output check returns True (deferred, phase 2)."""
        passed, detail = run_dod_check(
            sample_contract, EnumDodCheckType.RENDERED_OUTPUT
        )
        assert passed is True
        assert "deferred" in detail.lower()


# ---------------------------------------------------------------------------
# dispatch_lease.py — C4 fix
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDispatchLease:
    """File-based dispatch lease prevents concurrent dispatch."""

    def test_acquire_creates_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            acquire_dispatch_lease(state_dir, "tick-001", "test-holder")
            lock_path = os.path.join(state_dir, "dispatch-lock.json")
            assert os.path.isfile(lock_path)
            with open(lock_path) as f:
                data = json.load(f)
            assert data["tick_id"] == "tick-001"
            assert data["holder"] == "test-holder"
            release_dispatch_lease(state_dir)

    def test_release_removes_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            acquire_dispatch_lease(state_dir, "tick-002", "test-holder")
            release_dispatch_lease(state_dir)
            lock_path = os.path.join(state_dir, "dispatch-lock.json")
            assert not os.path.isfile(lock_path)

    def test_second_acquire_raises_when_lease_held(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            acquire_dispatch_lease(state_dir, "tick-003", "first-holder")
            with pytest.raises(DispatchLeaseHeld) as exc_info:
                acquire_dispatch_lease(state_dir, "tick-004", "second-holder")
            assert "first-holder" in str(exc_info.value)
            release_dispatch_lease(state_dir)

    def test_stale_lease_overwritten(self) -> None:
        """A lease older than LEASE_EXPIRY_SECONDS can be overwritten."""
        with tempfile.TemporaryDirectory() as state_dir:
            # Write a stale lease manually
            lock_path = os.path.join(state_dir, "dispatch-lock.json")
            old_ts = (
                datetime.now(tz=UTC) - timedelta(seconds=LEASE_EXPIRY_SECONDS + 60)
            ).isoformat()
            with open(lock_path, "w") as f:
                json.dump(
                    {
                        "tick_id": "old-tick",
                        "acquired_at": old_ts,
                        "holder": "old-holder",
                    },
                    f,
                )

            # Should NOT raise — stale lease is overwritten
            acquire_dispatch_lease(state_dir, "tick-new", "new-holder")
            data = read_dispatch_lease(state_dir)
            assert data is not None
            assert data["holder"] == "new-holder"
            release_dispatch_lease(state_dir)

    def test_context_manager_releases_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            with dispatch_lease(state_dir, "tick-ctx", "ctx-holder"):
                assert read_dispatch_lease(state_dir) is not None
            assert read_dispatch_lease(state_dir) is None

    def test_context_manager_releases_on_exception(self) -> None:
        """dispatch_lease context manager releases even when body raises."""
        with (
            tempfile.TemporaryDirectory() as state_dir,
            pytest.raises(RuntimeError),
            dispatch_lease(state_dir, "tick-err", "err-holder"),
        ):
            raise RuntimeError("simulated dispatch error")

    def test_context_manager_releases_on_exception_no_lock_after(self) -> None:
        """After dispatch_lease body raises, lock file is absent."""
        tmp = tempfile.mkdtemp()
        try:
            try:
                with dispatch_lease(tmp, "tick-err2", "err-holder2"):
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
            assert read_dispatch_lease(tmp) is None
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)

    def test_read_returns_none_when_no_lock(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            assert read_dispatch_lease(state_dir) is None

    def test_lease_age_returns_none_when_no_lock(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            assert lease_age(state_dir) is None

    def test_lease_age_returns_timedelta_when_held(self) -> None:
        with tempfile.TemporaryDirectory() as state_dir:
            acquire_dispatch_lease(state_dir, "tick-age", "age-holder")
            age = lease_age(state_dir)
            assert age is not None
            assert age.total_seconds() >= 0
            assert age.total_seconds() < 5  # just acquired
            release_dispatch_lease(state_dir)

    def test_make_tick_id_format(self) -> None:
        """make_tick_id produces tick-YYYYMMDD-HHMM format."""
        import re

        ts = datetime(2026, 4, 12, 3, 15, tzinfo=UTC)
        tick_id = make_tick_id(ts)
        assert tick_id == "tick-20260412-0315"
        assert re.match(r"tick-\d{8}-\d{4}", tick_id)

    def test_release_is_idempotent(self) -> None:
        """release_dispatch_lease on missing file doesn't raise."""
        with tempfile.TemporaryDirectory() as state_dir:
            # Should not raise even without prior acquire
            release_dispatch_lease(state_dir)


# ---------------------------------------------------------------------------
# build_pulse_prompt — CronOutputVerificationRoutine content
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildPulsePrompt:
    """build_pulse_prompt produces prompts with required Rev 7 content."""

    def test_prompt_contains_cross_tick_verification(self) -> None:
        prompt = build_pulse_prompt(
            cron_name="build-dispatch-pulse",
            timeout_budget_ms=300000,
            session_id="sess-001",
            state_dir=".onex_state",
        )
        assert "Cross-tick verification" in prompt
        assert "HALLUCINATED PASS" in prompt
        assert "dispatch-events" in prompt

    def test_prompt_contains_vacuous_pulse_gate(self) -> None:
        prompt = build_pulse_prompt(
            cron_name="build-dispatch-pulse",
            timeout_budget_ms=300000,
            session_id="sess-001",
            state_dir=".onex_state",
        )
        assert "VACUOUS_PULSE" in prompt
        assert "backlog_unworked_count" in prompt

    def test_prompt_contains_dispatch_lease_reference(self) -> None:
        prompt = build_pulse_prompt(
            cron_name="build-dispatch-pulse",
            timeout_budget_ms=300000,
            session_id="sess-001",
            state_dir=".onex_state",
        )
        assert "dispatch-lock.json" in prompt

    def test_prompt_uses_provided_state_dir(self) -> None:
        prompt = build_pulse_prompt(
            cron_name="build-dispatch-pulse",
            timeout_budget_ms=300000,
            session_id="sess-001",
            state_dir="/custom/state",
        )
        assert "/custom/state" in prompt

    def test_prompt_no_hardcoded_absolute_paths(self) -> None:
        """Prompt must not contain hardcoded /Users/jonah or /Volumes/ paths."""
        prompt = build_pulse_prompt(
            cron_name="build-dispatch-pulse",
            timeout_budget_ms=300000,
            session_id="sess-001",
            state_dir=".onex_state",
        )
        assert "/Users/jonah" not in prompt
        assert "/Volumes/" not in prompt

    def test_stall_thresholds_derived_from_budget(self) -> None:
        """Stall/dead thresholds in prompt are derived from timeout_budget_ms."""
        prompt = build_pulse_prompt(
            cron_name="build-dispatch-pulse",
            timeout_budget_ms=300000,  # 300s -> stall=99s, dead=198s
            session_id="sess-001",
            state_dir=".onex_state",
        )
        # 300 * 0.33 = 99, 300 * 0.66 = 198
        assert "99" in prompt
        assert "198" in prompt

    def test_generic_fallback_for_unknown_cron(self) -> None:
        prompt = build_pulse_prompt(
            cron_name="some-future-cron",
            timeout_budget_ms=120000,
            session_id="sess-002",
            state_dir=".onex_state",
        )
        assert "some-future-cron" in prompt
        assert "sess-002" in prompt
