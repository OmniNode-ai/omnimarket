# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared setup of the central test harness tests: a real git repository, a real lab host
on the in-memory bus, and an overlay whose cases run real commands at a real commit."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.lab_work.bus import LabWorkCaller, LabWorkHost
from omnimarket.models.lab_test_harness import ModelLabTestHarnessOverlay
from omnimarket.nodes.node_lab_test_harness_effect.handlers import (
    LabWorkCallerRunner,
)
from omnimarket.nodes.node_lab_work_unit_effect import (
    HandlerHostCapacityAdvertiseEffect,
    HandlerLabWorkUnitEffect,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols import (
    HostReading,
    LocalShellLabWorkExecutor,
)

HOST = "h202"
REPO = "OmniNode-ai/demo"
T0 = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()


class _Reader:
    def read(self, tools: list[str]) -> HostReading:
        return HostReading(
            cores=8, load1=0.1, mem_available_bytes=8 * 1024**3, tools=("git",)
        )


def make_origin(tmp_path: Path) -> tuple[str, str]:
    """A repository with a passing probe and a failing probe; returns (url_base, sha)."""
    origin = tmp_path / "origin" / "OmniNode-ai" / "demo"
    origin.mkdir(parents=True)
    _git("init", "-q", cwd=origin)
    (origin / "passes.py").write_text("print('ok')\n")
    (origin / "fails.py").write_text("import sys\nprint('broken input')\nsys.exit(3)\n")
    _git("add", ".", cwd=origin)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x", cwd=origin)
    sha = _git("rev-parse", "HEAD", cwd=origin)
    (origin.parent / "demo.git").symlink_to(origin / ".git")
    return f"file://{tmp_path / 'origin'}", sha


def overlay_document(
    sha: str,
    result_path: Path,
    *,
    golden: str = "passes.py",
    error: str = "fails.py",
    host: str = HOST,
    receipt_slack_seconds: int = 5,
    max_age_seconds: int = 900,
) -> dict[str, object]:
    python = sys.executable
    return {
        "overlay_revision": "test.1",
        "piece": "central-test-harness",
        "tag": "drill",
        "backend": "inmemory",
        "lane": "harness-drill",
        "schedule": {"workflow": "lab-test-harness", "interval_seconds": 300},
        "max_age_seconds": max_age_seconds,
        "receipt_slack_seconds": receipt_slack_seconds,
        "current_result_path": str(result_path),
        "cases": [
            {
                "case_id": "golden-passes",
                "kind": "golden",
                "repo": REPO,
                "commit_sha": sha,
                "argv": [python, golden],
                "target_host": host,
                "timeout_seconds": 60,
            },
            {
                "case_id": "error-broken-input",
                "kind": "error",
                "repo": REPO,
                "commit_sha": sha,
                "argv": [python, error],
                "target_host": host,
                "timeout_seconds": 60,
            },
        ],
    }


def make_overlay(
    sha: str, result_path: Path, **kwargs: object
) -> ModelLabTestHarnessOverlay:
    return ModelLabTestHarnessOverlay.model_validate(
        overlay_document(sha, result_path, **kwargs)  # type: ignore[arg-type]
    )


class LiveLabBus:
    """An in-memory bus with a lab host serving it (or, with serve=False, no host at all)."""

    def __init__(self, url_base: str, work_root: Path, *, serve: bool = True) -> None:
        self._url_base, self._work_root, self._serve = url_base, work_root, serve

    @contextlib.asynccontextmanager
    async def opener(
        self, overlay: ModelLabTestHarnessOverlay
    ) -> AsyncIterator[LabWorkCallerRunner]:
        bus = EventBusInmemory(environment="local", group="lab-test-harness-test")
        await bus.start()
        host: LabWorkHost | None = None
        if self._serve:
            executor = LocalShellLabWorkExecutor(
                self._work_root, url_base=self._url_base
            )
            host = LabWorkHost(
                bus,
                HandlerLabWorkUnitEffect(
                    HOST,
                    executor,
                    allowed_executables=frozenset({Path(sys.executable).name}),
                ),
                HandlerHostCapacityAdvertiseEffect(_Reader()),
            )
            await host.start(advertise=False)
        caller = LabWorkCaller(bus, wait_slack_seconds=overlay.receipt_slack_seconds)
        await caller.start()
        try:
            yield LabWorkCallerRunner(caller)
        finally:
            await caller.stop()
            if host is not None:
                await host.stop()
            await bus.close()
