# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixtures: a lab tree with old and fresh history, a Drive that stores what it is given."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from omnimarket.nodes.node_lab_history_archive_effect.adapters import subprocess_runner
from omnimarket.nodes.node_lab_history_archive_effect.models import (
    ModelArchiveBudget,
    ModelArchiveDrive,
    ModelArchiveHost,
    ModelArchiveSource,
    ModelLabHistoryArchiveDeployment,
)
from omnimarket.nodes.node_lab_history_archive_effect.protocols import (
    DriveUploadError,
    ModelDriveObject,
)

AS_OF = dt.datetime(2026, 10, 17, 6, 0, tzinfo=dt.UTC)
OLD = (AS_OF - dt.timedelta(days=10)).timestamp()
FRESH = (AS_OF - dt.timedelta(days=3)).timestamp()


def write(path: Path, data: bytes, mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


class FakeDrive:
    """Stores uploaded objects by remote dir and name; the md5 it reports is its own."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.uploads: list[tuple[str, str, int]] = []
        self.ready: tuple[str, str] | None = None
        self.corrupt_md5 = False
        self.fail_upload = False
        self.hide_objects = False
        self.on_upload: Callable[[Path], None] | None = None

    def check_ready(self) -> tuple[str, str] | None:
        return self.ready

    def upload(self, local: Path, remote_dir: str, bytes_per_second: int) -> None:
        if self.fail_upload:
            raise DriveUploadError("rclone copyto exit 1: quota")
        self.uploads.append((remote_dir, local.name, bytes_per_second))
        self.objects[(remote_dir, local.name)] = local.read_bytes()
        if self.on_upload is not None:
            self.on_upload(local)

    def remote_object(self, remote_dir: str, name: str) -> ModelDriveObject | None:
        data = self.objects.get((remote_dir, name))
        if data is None or self.hide_objects:
            return None
        md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
        return ModelDriveObject(
            name=name, size=len(data), md5="0" * 32 if self.corrupt_md5 else md5
        )


class RecordingRunner:
    """Runs the real command and keeps every argv, so a test can read the throttle off it."""

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []

    def __call__(
        self,
        argv: Sequence[str],
        env: Mapping[str, str] | None = None,
        timeout: int = 1800,
    ) -> subprocess.CompletedProcess[str]:
        self.argvs.append(list(argv))
        return subprocess_runner(argv, env, timeout)


@pytest.fixture
def lab(tmp_path: Path) -> dict[str, Path]:
    sessions = tmp_path / "boot" / "sessions"
    reports = tmp_path / "boot" / "reports"
    write(sessions / "a.jsonl", b"A" * 4000, OLD)
    write(sessions / "b.jsonl", b"B" * 3000, OLD)
    write(sessions / "fresh.jsonl", b"F" * 100, FRESH)
    write(sessions / "memory" / "notes.md", b"keep", OLD)
    write(sessions / "MEMORY.md", b"index", OLD)
    write(reports / "ROLLING_WORK_LEDGER.md", b"live ledger", OLD)
    write(
        reports / "archive" / "ROLLING_WORK_LEDGER_2026-10-12-split.md", b"detail", OLD
    )
    write(reports / "r1.md", b"report", OLD)
    return {
        "sessions": sessions,
        "reports": reports,
        "staging": tmp_path / "second-drive" / "staging",
    }


@pytest.fixture
def deployment(lab: dict[str, Path]) -> ModelLabHistoryArchiveDeployment:
    return ModelLabHistoryArchiveDeployment(
        hosts=(
            ModelArchiveHost(id="h101", hostnames=("h101.lab",), roles=("lab",)),
            ModelArchiveHost(
                id="h200",
                hostnames=("h200.lab",),
                roles=("landing_controller", "pr_watcher"),
            ),
        ),
        sources=(
            ModelArchiveSource(
                name="sessions", root=lab["sessions"], include=("*.jsonl",)
            ),
            ModelArchiveSource(name="reports", root=lab["reports"]),
        ),
        staging_dir=lab["staging"],
        drive=ModelArchiveDrive(remote="lab-archive", folder="OmniNode Lab Archive"),
        budget=ModelArchiveBudget(bytes_per_second=2_000_000),
    )


@pytest.fixture
def drive() -> FakeDrive:
    return FakeDrive()


def distinct_devices(path: Path) -> int:
    """Staging reads as a second drive: every path under it has its own device id."""
    return 2 if "second-drive" in path.parts else 1
