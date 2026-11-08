# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review findings of the drain node (OMN-22905): uncertainty is kept, store failures end the chain."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_host_drain_effect.adapters.fakes import FixedLaneSource
from omnimarket.nodes.node_host_drain_effect.adapters.runner_ports import (
    CommandWorkerProbe,
    ProcWorkerProbe,
)
from omnimarket.nodes.node_host_drain_effect.adapters.stores import InMemoryDrainStore
from omnimarket.nodes.node_host_drain_effect.handlers.handler_host_drain import (
    HandlerHostDrain,
)
from omnimarket.nodes.node_host_drain_effect.models.model_host_drain import (
    EnumDrainOp,
    EnumDrainOutcome,
    EnumRunnerActivity,
    ModelHostDrainRequest,
)
from omnimarket.nodes.node_host_drain_effect.overlay import (
    ModelHostDrainOverlay,
    load_example_overlay,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)
RUNNER = "example-runner-1"
RUNNER_DIR = "/example/runners/example-runner-1"


def _proc_table(root: Path) -> ProcWorkerProbe:
    overlay = load_example_overlay()
    return ProcWorkerProbe(
        overlay.runner_stop, proc_root=root, runner_dirs={RUNNER: RUNNER_DIR}
    )


def test_a_process_entry_that_cannot_be_read_keeps_the_runner_unknown(
    tmp_path: Path,
) -> None:
    # A visible process whose command line cannot be read (here: not a file) may be a live worker.
    (tmp_path / "4242").mkdir()
    (tmp_path / "4242" / "cmdline").mkdir()
    probe = _proc_table(tmp_path)
    assert probe.activity(RUNNER) is EnumRunnerActivity.UNKNOWN
    assert probe.is_running(RUNNER) is None


def test_a_process_that_vanished_while_listing_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "4243").mkdir()  # exited between the listing and the read: no cmdline
    (tmp_path / "4244").mkdir()
    (tmp_path / "4244" / "cmdline").write_bytes(
        f"{RUNNER_DIR}/bin/Runner.Worker\0--x\0".encode()
    )
    probe = _proc_table(tmp_path)
    assert probe.activity(RUNNER) is EnumRunnerActivity.BUSY
    assert probe.is_running(RUNNER) is True


def _done(code: int, out: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout=out, stderr="")


@pytest.mark.parametrize("output", ["", "garbage", "maybe"])
def test_command_probe_output_that_names_neither_state_is_not_a_confirmed_shutdown(
    overlay: ModelHostDrainOverlay, output: str
) -> None:
    probe = CommandWorkerProbe(
        overlay.runner_stop, run=lambda *_a, **_k: _done(0, output)
    )
    assert probe.is_running(RUNNER) is None


def test_command_probe_reads_the_declared_tokens_for_up_and_down(
    overlay: ModelHostDrainOverlay,
) -> None:
    settings = overlay.runner_stop.command_probe
    up = CommandWorkerProbe(
        overlay.runner_stop, run=lambda *_a, **_k: _done(0, settings.running_token)
    )
    down = CommandWorkerProbe(
        overlay.runner_stop, run=lambda *_a, **_k: _done(0, settings.stopped_token)
    )
    assert up.is_running(RUNNER) is True
    assert down.is_running(RUNNER) is False


class _UnreadableLanes:
    def running(self, host_name: str) -> int:
        raise ValueError("not an integer")


def _drain(
    overlay: ModelHostDrainOverlay,
    store: Any,
    lanes: Any,
    sink: InMemoryEmissionSink,
    op: EnumDrainOp = EnumDrainOp.DRAIN,
) -> Any:
    handler = HandlerHostDrain(
        overlay.drain,
        store=store,
        lanes=lanes,
        sink=sink,
        clock=lambda: NOW,
        backend="in_memory",
    )
    return handler.handle(ModelHostDrainRequest(op=op, host_name="example-host-a"))


def test_an_unreadable_lane_count_is_not_published_as_zero(
    overlay: ModelHostDrainOverlay,
) -> None:
    sink = InMemoryEmissionSink()
    result = _drain(overlay, InMemoryDrainStore(), _UnreadableLanes(), sink)
    assert result.lanes_running is None
    assert result.drained is False
    success = [e for e in sink.events if e.phase == "success"]
    assert len(success) == 1
    assert "lanes_running" not in success[0].counters
    assert success[0].counters["lanes_unreadable"] == 1


def test_a_measured_zero_is_still_a_zero(overlay: ModelHostDrainOverlay) -> None:
    sink = InMemoryEmissionSink()
    result = _drain(
        overlay, InMemoryDrainStore(), FixedLaneSource({"example-host-a": 0}), sink
    )
    assert result.lanes_running == 0
    assert result.drained is True
    success = [e for e in sink.events if e.phase == "success"]
    assert success[0].counters["lanes_running"] == 0
    assert "lanes_unreadable" not in success[0].counters


class _WriteFailsStore(InMemoryDrainStore):
    def set_draining(self, *args: Any, **kwargs: Any) -> None:
        raise PermissionError("state file")


class _ReadFailsStore(InMemoryDrainStore):
    def is_draining(self, host_name: str) -> bool:
        raise RuntimeError("broker gone")


def _phases(sink: InMemoryEmissionSink) -> list[str]:
    return [e.phase for e in sink.events]


def test_a_store_write_failure_ends_the_chain_with_a_typed_failure(
    overlay: ModelHostDrainOverlay,
) -> None:
    sink = InMemoryEmissionSink()
    result = _drain(
        overlay, _WriteFailsStore(), FixedLaneSource({"example-host-a": 1}), sink
    )
    assert result.outcome is EnumDrainOutcome.FAILED
    assert result.failure_code == overlay.drain.codes.write_failed
    assert _phases(sink) == ["start", "failure"]


def test_a_failed_readback_after_a_write_is_told_apart_from_a_failed_write(
    overlay: ModelHostDrainOverlay,
) -> None:
    sink = InMemoryEmissionSink()
    result = _drain(
        overlay, _ReadFailsStore(), FixedLaneSource({"example-host-a": 1}), sink
    )
    assert result.outcome is EnumDrainOutcome.FAILED
    assert result.failure_code == overlay.drain.codes.readback_failed
    assert "write" in result.detail
    assert _phases(sink) == ["start", "failure"]


def test_a_store_read_failure_on_a_status_or_start_check_ends_the_chain(
    overlay: ModelHostDrainOverlay,
) -> None:
    for op in (EnumDrainOp.STATUS, EnumDrainOp.CHECK_START):
        sink = InMemoryEmissionSink()
        result = _drain(
            overlay, _ReadFailsStore(), FixedLaneSource({"example-host-a": 1}), sink, op
        )
        assert result.outcome is EnumDrainOutcome.FAILED
        assert result.admitted is not True
        assert _phases(sink) == ["start", "failure"]
