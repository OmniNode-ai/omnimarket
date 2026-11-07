# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay and the real-backend profile of node_host_drain_effect (OMN-22905)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_host_drain_effect.adapters.runner_ports import (
    CommandLaneSource,
    CommandWorkerStopper,
)
from omnimarket.nodes.node_host_drain_effect.adapters.stores import RedpandaDrainStore
from omnimarket.nodes.node_host_drain_effect.overlay import (
    EXAMPLE_OVERLAY,
    NODE_NAME,
    ModelHostDrainOverlay,
    load_overlay,
)

NOW = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)


class _FakeTopic:
    """A compacted topic held in a list: what the broker would return to a reader from the start."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, bytes]] = []

    async def send(self, topic: str, key: str, value: bytes) -> None:
        self.rows.append((key, value))

    async def read_all(self, topic: str) -> list[tuple[str, bytes]]:
        return list(self.rows)


def test_redpanda_profile_keeps_the_latest_record_per_host(
    overlay: ModelHostDrainOverlay,
) -> None:
    store = RedpandaDrainStore(overlay.redpanda, _FakeTopic())
    store.set_draining("example-host-a", True, reason="window", at=NOW)
    store.set_draining("example-host-b", True, reason="window", at=NOW)
    store.set_draining("example-host-b", False, reason="back", at=NOW)
    assert store.is_draining("example-host-a") is True
    assert store.is_draining("example-host-b") is False
    assert store.draining_hosts() == ("example-host-a",)


def test_overlay_with_a_missing_key_refuses_the_load(tmp_path: Path) -> None:
    document = yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8"))
    del document["runner_stop"]["stop_timeout_seconds"]
    node_dir = tmp_path / NODE_NAME
    node_dir.mkdir()
    (node_dir / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(NodeOverlayError):
        load_overlay({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})


def test_overlay_loads_from_a_root_and_picks_the_profile(tmp_path: Path) -> None:
    document = yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8"))
    document["profile"] = "redpanda"
    node_dir = tmp_path / NODE_NAME
    node_dir.mkdir()
    (node_dir / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    loaded = load_overlay({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
    assert loaded.profile == "redpanda"


def test_overlay_runner_on_an_undeclared_host_is_refused() -> None:
    document = yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8"))
    document["runner_stop"]["runners"][0]["host"] = "other-host"
    with pytest.raises(ValueError, match=r"not in drain\.hosts"):
        ModelHostDrainOverlay.model_validate(document)


def _done(code: int, out: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=out)


def test_command_stopper_runs_the_overlay_argv_with_the_unit(
    overlay: ModelHostDrainOverlay,
) -> None:
    seen: list[list[str]] = []

    def run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return _done(0)

    assert CommandWorkerStopper(overlay.runner_stop, run=run).stop("example-runner-1")
    assert seen == [["example-service-control", "stop", "example-runner-1.service"]]
    failing = CommandWorkerStopper(overlay.runner_stop, run=lambda *_a, **_k: _done(3))
    assert failing.stop("example-runner-1") is False
    assert failing.stop("not-a-runner") is False


def test_command_lane_source_reads_the_integer_the_command_prints(
    overlay: ModelHostDrainOverlay,
) -> None:
    lanes = CommandLaneSource(overlay.lanes, run=lambda *_a, **_k: _done(0, "3\n"))
    assert lanes.running("example-host-a") == 3


def test_command_probe_reads_busy_idle_and_unknown_from_the_argv_output(
    overlay: ModelHostDrainOverlay,
) -> None:
    from omnimarket.nodes.node_host_drain_effect.adapters.runner_ports import (
        CommandWorkerProbe,
    )
    from omnimarket.nodes.node_host_drain_effect.models.model_host_drain import (
        EnumRunnerActivity,
    )

    probe_settings = overlay.runner_stop.command_probe

    def probe_with(code: int, out: str) -> CommandWorkerProbe:
        return CommandWorkerProbe(
            overlay.runner_stop, run=lambda *_a, **_k: _done(code, out)
        )

    assert (
        probe_with(0, probe_settings.busy_token + "\n").activity("example-runner-1")
        is EnumRunnerActivity.BUSY
    )
    assert (
        probe_with(0, probe_settings.idle_token).activity("example-runner-1")
        is EnumRunnerActivity.IDLE
    )
    assert (
        probe_with(0, "garbage").activity("example-runner-1")
        is EnumRunnerActivity.UNKNOWN
    )
    # A command that fails reads UNKNOWN, never idle: the stop fails closed.
    assert (
        probe_with(1, probe_settings.idle_token).activity("example-runner-1")
        is EnumRunnerActivity.UNKNOWN
    )
    assert (
        probe_with(0, probe_settings.running_token).is_running("example-runner-1")
        is True
    )
    assert probe_with(0, "false").is_running("example-runner-1") is False
    assert probe_with(1, "").is_running("example-runner-1") is None
