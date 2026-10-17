# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live adapters: a throttled command runner and an rclone Drive uploader (OMN-17427).

The uploader is a CLI (rclone) pointed at a configured remote, never Google Drive for
desktop: Drive for desktop indexes and syncs the whole tree and drove a lab host's load
to 60 on 24 cores. Every command the archive starts carries nice, an IO class and (for
the upload) a bytes-per-second cap. This module reads no credential: the remote's
config belongs to rclone, and `check_ready` only asks whether the remote exists.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath

from .models import ModelArchiveBudget, ModelLabHistoryArchivePartEvent
from .protocols import DriveUploadError, ModelDriveObject, ProtocolCommandRunner


def throttle_prefix(
    budget: ModelArchiveBudget,
    *,
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[str, ...]:
    """Argv prefix that lowers CPU and IO priority of whatever follows it."""
    platform = platform or sys.platform
    prefix: list[str] = ["nice", "-n", str(budget.nice)]
    if platform == "darwin":
        if which("taskpolicy"):
            prefix += ["taskpolicy", "-b"]
    elif which("ionice"):
        prefix += (
            ["ionice", "-c", "3"]
            if budget.io_class == "idle"
            else ["ionice", "-c", "2", "-n", "7"]
        )
    return tuple(prefix)


def subprocess_runner(
    argv: Sequence[str],
    env: Mapping[str, str] | None = None,
    timeout: int = 1800,
) -> subprocess.CompletedProcess[str]:
    import os

    merged = {**os.environ, **(env or {})}
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=merged,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(list(argv), 124, "", f"timed out: {exc}")
    except OSError as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))


class RcloneDriveUploader:
    """Uploads with `rclone copyto` under a bandwidth cap; reads Drive's md5 with `lsjson`."""

    def __init__(
        self,
        *,
        remote: str,
        budget: ModelArchiveBudget,
        runner: ProtocolCommandRunner = subprocess_runner,
        which: Callable[[str], str | None] = shutil.which,
        platform: str | None = None,
    ) -> None:
        self._remote = remote
        self._budget = budget
        self._runner = runner
        self._which = which
        self._prefix = throttle_prefix(budget, platform=platform, which=which)

    def _target(self, remote_dir: str) -> str:
        return f"{self._remote}:{PurePosixPath(remote_dir)}"

    def check_ready(self) -> tuple[str, str] | None:
        if self._which("rclone") is None:
            return ("UPLOADER_UNAVAILABLE", "rclone is not installed on this host")
        proc = self._runner([*self._prefix, "rclone", "listremotes"], None, 60)
        if proc.returncode != 0:
            return ("UPLOADER_UNAVAILABLE", "rclone listremotes failed")
        if f"{self._remote}:" not in proc.stdout.split():
            return (
                "CREDENTIAL_MISSING",
                f"no rclone remote named {self._remote} is configured on this host",
            )
        return None

    def upload(self, local: Path, remote_dir: str, bytes_per_second: int) -> None:
        kib = max(1, bytes_per_second // 1024)
        argv = [
            *self._prefix,
            "rclone",
            "copyto",
            str(local),
            f"{self._target(remote_dir)}/{local.name}",
            "--bwlimit",
            f"{kib}k",
            "--transfers",
            "1",
            "--checkers",
            "1",
            "--drive-chunk-size",
            "8M",
            "--retries",
            "3",
            "--stats",
            "0",
        ]
        proc = self._runner(argv, {"GOMAXPROCS": "1"}, self._budget.max_run_seconds)
        if proc.returncode != 0:
            raise DriveUploadError(
                f"rclone copyto exit {proc.returncode}: {proc.stderr.strip()[-300:]}"
            )

    def remote_object(self, remote_dir: str, name: str) -> ModelDriveObject | None:
        argv = [
            *self._prefix,
            "rclone",
            "lsjson",
            "--hash",
            "--files-only",
            "--no-modtime",
            self._target(remote_dir),
        ]
        proc = self._runner(argv, {"GOMAXPROCS": "1"}, 600)
        if proc.returncode != 0:
            raise DriveUploadError(
                f"rclone lsjson exit {proc.returncode}: {proc.stderr.strip()[-300:]}"
            )
        try:
            entries = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise DriveUploadError(
                f"rclone lsjson returned unreadable output: {exc}"
            ) from None
        for entry in entries:
            if entry.get("Name") == name:
                return ModelDriveObject(
                    name=name,
                    size=int(entry["Size"]),
                    md5=str((entry.get("Hashes") or {}).get("md5", "")).lower(),
                )
        return None


class BusPartEventSink:
    """Publishes each part event on the topic the caller names (the contract's part topic)."""

    def __init__(
        self, publish: Callable[[str, bytes, bytes], None], topic: str
    ) -> None:
        self._publish = publish
        self._topic = topic

    def emit(self, event: ModelLabHistoryArchivePartEvent) -> None:
        self._publish(
            self._topic,
            f"{event.host}:{event.part}".encode(),
            event.model_dump_json().encode(),
        )
