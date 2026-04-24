"""OMN-9578: concurrent-run guard for node_close_out.

Verifies:
  * Second concurrent invocation receives ModelCloseOutSkipped with
    reason='concurrent_run_in_progress' while the first run still holds the
    lease.
  * Stale lease (older than 2x expected duration) is reclaimable by a
    subsequent run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_close_out.close_out_lease import (
    LEASE_EXPIRY_SECONDS,
    CloseOutLeaseHeldError,
    acquire_close_out_lease,
    read_close_out_lease,
    release_close_out_lease,
)
from omnimarket.nodes.node_close_out.handlers.handler_close_out import (
    HandlerCloseOut,
)
from omnimarket.nodes.node_close_out.models.model_close_out_completed_event import (
    ModelCloseOutCompletedEvent,
)
from omnimarket.nodes.node_close_out.models.model_close_out_skipped import (
    ModelCloseOutSkipped,
)
from omnimarket.nodes.node_close_out.models.model_close_out_start_command import (
    ModelCloseOutStartCommand,
)


def _make_command() -> ModelCloseOutStartCommand:
    return ModelCloseOutStartCommand(
        correlation_id=uuid4(),
        dry_run=True,
        requested_at=datetime.now(tz=UTC),
    )


@pytest.mark.unit
class TestConcurrentRunGuard:
    def test_second_concurrent_invocation_is_skipped(self, tmp_path: Path) -> None:
        """Guard rejects the second invocation while the first holds the lease."""
        state_dir = str(tmp_path)

        acquire_close_out_lease(state_dir, "first-run-cid", "test-first")

        try:
            handler = HandlerCloseOut()
            command = _make_command()

            result = handler.handle_with_guard(
                command, state_dir=state_dir, holder="test-second"
            )

            assert isinstance(result, ModelCloseOutSkipped)
            assert result.reason == "concurrent_run_in_progress"
            assert result.correlation_id == command.correlation_id
            assert result.holder == "test-first"
            assert result.holder_acquired_at is not None

            lease = read_close_out_lease(state_dir)
            assert lease is not None
            assert lease["correlation_id"] == "first-run-cid"
        finally:
            release_close_out_lease(state_dir, correlation_id="first-run-cid")

    def test_first_invocation_runs_to_completion_and_releases(
        self, tmp_path: Path
    ) -> None:
        """First invocation acquires, runs, and releases the lease."""
        state_dir = str(tmp_path)
        handler = HandlerCloseOut()
        command = _make_command()

        result = handler.handle_with_guard(command, state_dir=state_dir)

        assert isinstance(result, ModelCloseOutCompletedEvent)
        assert result.correlation_id == command.correlation_id
        assert read_close_out_lease(state_dir) is None

    def test_sequential_invocations_both_succeed(self, tmp_path: Path) -> None:
        """Once the first run releases, the next invocation runs normally."""
        state_dir = str(tmp_path)
        handler = HandlerCloseOut()

        first = handler.handle_with_guard(_make_command(), state_dir=state_dir)
        second = handler.handle_with_guard(_make_command(), state_dir=state_dir)

        assert isinstance(first, ModelCloseOutCompletedEvent)
        assert isinstance(second, ModelCloseOutCompletedEvent)

    def test_raw_lease_rejects_second_acquire_while_held(self, tmp_path: Path) -> None:
        """Lease primitive raises CloseOutLeaseHeldError on second acquire."""
        state_dir = str(tmp_path)
        acquire_close_out_lease(state_dir, "cid-a", "holder-a")
        try:
            with pytest.raises(CloseOutLeaseHeldError) as exc:
                acquire_close_out_lease(state_dir, "cid-b", "holder-b")
            assert exc.value.holder == "holder-a"
        finally:
            release_close_out_lease(state_dir, correlation_id="cid-a")


@pytest.mark.unit
class TestStaleLeaseReclamation:
    def test_stale_lease_older_than_expiry_is_reclaimed(self, tmp_path: Path) -> None:
        """A lease acquired LEASE_EXPIRY_SECONDS + 1 ago is reclaimable."""
        state_dir = str(tmp_path)

        stale_acquired_at = datetime.now(tz=UTC) - timedelta(
            seconds=LEASE_EXPIRY_SECONDS + 1
        )
        acquire_close_out_lease(
            state_dir,
            correlation_id="stale-run-cid",
            holder="crashed-run",
            now=stale_acquired_at,
        )

        handler = HandlerCloseOut()
        command = _make_command()

        result = handler.handle_with_guard(
            command, state_dir=state_dir, holder="fresh-run"
        )

        assert isinstance(result, ModelCloseOutCompletedEvent)
        assert result.correlation_id == command.correlation_id
        assert read_close_out_lease(state_dir) is None

    def test_stale_lease_below_expiry_is_not_reclaimed(self, tmp_path: Path) -> None:
        """A lease younger than LEASE_EXPIRY_SECONDS still blocks new runs."""
        state_dir = str(tmp_path)

        young_acquired_at = datetime.now(tz=UTC) - timedelta(
            seconds=LEASE_EXPIRY_SECONDS - 30
        )
        acquire_close_out_lease(
            state_dir,
            correlation_id="young-run-cid",
            holder="in-flight-run",
            now=young_acquired_at,
        )
        try:
            handler = HandlerCloseOut()
            result = handler.handle_with_guard(
                _make_command(), state_dir=state_dir, holder="new-run"
            )
            assert isinstance(result, ModelCloseOutSkipped)
            assert result.reason == "concurrent_run_in_progress"
            assert result.holder == "in-flight-run"
        finally:
            release_close_out_lease(state_dir, correlation_id="young-run-cid")

    def test_corrupt_lease_file_is_reclaimable(self, tmp_path: Path) -> None:
        """A lease file with unparseable JSON is treated as reclaimable."""
        state_dir = str(tmp_path)
        lock_path = tmp_path / "close-out-lock.json"
        lock_path.write_text("{not valid json")

        handler = HandlerCloseOut()
        command = _make_command()
        result = handler.handle_with_guard(command, state_dir=state_dir)

        assert isinstance(result, ModelCloseOutCompletedEvent)
        assert read_close_out_lease(state_dir) is None


@pytest.mark.unit
class TestLeaseReleaseSafety:
    def test_release_does_not_remove_lease_with_different_correlation_id(
        self, tmp_path: Path
    ) -> None:
        """A straggler release() from a prior run cannot delete a newer lease."""
        state_dir = str(tmp_path)
        acquire_close_out_lease(state_dir, correlation_id="new-cid", holder="new")

        release_close_out_lease(state_dir, correlation_id="old-cid")

        lease = read_close_out_lease(state_dir)
        assert lease is not None
        assert lease["correlation_id"] == "new-cid"

        release_close_out_lease(state_dir, correlation_id="new-cid")
        assert read_close_out_lease(state_dir) is None

    def test_release_nonexistent_lease_is_noop(self, tmp_path: Path) -> None:
        """Release on missing lease does not raise."""
        release_close_out_lease(str(tmp_path), correlation_id="nope")

    def test_lease_payload_contains_pid_and_holder(self, tmp_path: Path) -> None:
        """Lease payload records pid and holder for operator introspection."""
        state_dir = str(tmp_path)
        acquire_close_out_lease(state_dir, correlation_id="c", holder="h")
        try:
            raw = (tmp_path / "close-out-lock.json").read_text()
            payload = json.loads(raw)
            assert payload["correlation_id"] == "c"
            assert payload["holder"] == "h"
            assert isinstance(payload["pid"], int)
        finally:
            release_close_out_lease(state_dir, correlation_id="c")
