# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reclaim disposable runner data only at a synchronous job lifecycle boundary.

The caller attests that job steps have stopped and supplies every active workspace.
A timer or a shared runtime must never infer quiescence from an old job log. The
runner-local adapter owns that attestation; a cleanup lock serializes callbacks.
"""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import stat
import sys
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import Literal

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_runner_workspace_retention_effect.models.model_runner_retention import (
    ModelRunnerRetentionPolicy,
    ModelRunnerRetentionRequest,
    ModelRunnerRetentionResult,
)


def _component(name: str) -> bool:
    return bool(name) and name not in {".", ".."} and Path(name).name == name


def _plain_directory(path: Path) -> Path:
    """Reject path aliases, including a symlink in an ancestor."""
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise ValueError(f"directory is not an absolute non-symlink path: {path}")
    if not path.is_dir():
        raise ValueError(f"not a directory: {path}")
    return path


def _mount_points() -> tuple[Path, ...]:
    """Include same-device bind mounts, which ismount alone cannot identify."""
    if sys.platform != "linux":
        raise ValueError("runner retention requires the Linux mount table")
    points = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        if len(fields) < 6:
            raise ValueError("runner mount table is malformed")
        name = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4])
        points.append(Path(name))
    if not points:
        raise ValueError("runner mount table is empty")
    return tuple(points)


def _size(path: Path, device: int) -> int:
    """Apparent regular-file bytes; never follow a link or traverse a mount."""
    info = path.lstat()
    if info.st_dev != device or (stat.S_ISDIR(info.st_mode) and path.is_mount()):
        raise ValueError(f"nested mount or filesystem boundary: {path}")
    if stat.S_ISLNK(info.st_mode):
        return 0
    if stat.S_ISREG(info.st_mode):
        return info.st_size
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"non-file runner data: {path}")
    return sum(_size(child, device) for child in path.iterdir())


def _identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    return info.st_dev, info.st_ino


class HandlerRunnerRetention:
    """Contract handler; roots and retention bounds come from the typed overlay."""

    def handle(
        self, request: ModelRunnerRetentionRequest
    ) -> ModelRunnerRetentionResult:
        if not request.lifecycle_boundary:
            return self._result(request, "refused", "lifecycle boundary is unverified")
        if request.active_workspaces:
            return self._result(request, "refused", "active workspace reported")
        try:
            return self._locked(request)
        except ValueError as exc:
            return self._result(request, "refused", str(exc))
        except OSError as exc:
            return self._result(request, "failed", str(exc))

    @staticmethod
    def _result(
        request: ModelRunnerRetentionRequest,
        status: Literal["completed", "refused", "locked", "failed"],
        reason: str,
    ) -> ModelRunnerRetentionResult:
        return ModelRunnerRetentionResult(
            status=status, reason=reason, dry_run=request.dry_run
        )

    def _locked(
        self, request: ModelRunnerRetentionRequest
    ) -> ModelRunnerRetentionResult:
        policy = load_structured_overlay(
            "node_runner_workspace_retention_effect",
            ModelRunnerRetentionPolicy,
            explicit=request.policy,
            file_env="RUNNER_RETENTION_POLICY_FILE",
        )
        names = (*policy.cache_budgets, *policy.protected_directories)
        if not _component(policy.work_directory) or not all(map(_component, names)):
            raise ValueError("policy paths must be direct child names")
        if set(policy.cache_budgets) & set(policy.protected_directories):
            raise ValueError("a protected directory cannot be a disposable cache")
        # A deployment overlay may add protections but may not turn runner
        # control state into a cache, regardless of spelling in the request.
        forbidden = {
            ".runner",
            ".credentials",
            ".credentials_rsaparams",
            "_actions",
            "_temp",
            "_PipelineMapping",
        }
        if set(policy.cache_budgets) & forbidden:
            raise ValueError("runner control paths cannot be disposable caches")
        home = _plain_directory(Path(request.runner_home))
        control = home / ".runner"
        if control.is_symlink() or not control.is_file():
            raise ValueError("runner identity file is absent or is a symlink")
        work = _plain_directory(home / policy.work_directory)
        if Path(__file__).resolve().is_relative_to(work) or Path(
            sys.prefix
        ).resolve().is_relative_to(work):
            raise ValueError(
                "node runtime must be installed outside disposable runner data"
            )
        mounts = _mount_points()
        if any(point != work and point.is_relative_to(work) for point in mounts):
            raise ValueError("nested mount in runner work root")
        workspace = Path(request.workspace)
        if not workspace.is_absolute() or workspace != workspace.resolve():
            raise ValueError("workspace is not an absolute non-symlink path")
        relative = workspace.relative_to(work)
        if len(relative.parts) != 2 or relative.parts[0] != relative.parts[1]:
            raise ValueError(
                "workspace must have the runner repository/repository shape"
            )
        if relative.parts[0] in set(names) | forbidden or relative.parts[0].startswith(
            (".", "_")
        ):
            raise ValueError("workspace overlaps cache or runner control data")
        if workspace.exists():
            _plain_directory(workspace)
        if not shutil.rmtree.avoids_symlink_attacks:
            raise ValueError("safe directory-relative removal is unavailable")
        with ExitStack() as stack:
            lock_fd = os.open(
                home / ".workspace-retention.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            stack.callback(os.close, lock_fd)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return self._result(
                    request, "locked", "another lifecycle cleanup holds the lock"
                )
            work_fd = os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, work_fd)
            if (os.fstat(work_fd).st_dev, os.fstat(work_fd).st_ino) != _identity(work):
                raise ValueError("work root changed before planning")
            return self._clean(request, policy, work, work_fd, forbidden)

    def _clean(
        self,
        request: ModelRunnerRetentionRequest,
        policy: ModelRunnerRetentionPolicy,
        work: Path,
        work_fd: int,
        forbidden: set[str],
    ) -> ModelRunnerRetentionResult:
        device = os.fstat(work_fd).st_dev
        protected = set(policy.protected_directories) | forbidden
        candidates: list[tuple[Path, int, tuple[int, int]]] = []
        preserved: list[str] = []
        before = 0
        for entry in sorted(work.iterdir()):
            name = entry.name
            if entry.is_symlink():
                preserved.append(name)
                continue
            size = _size(entry, device)
            before += size
            if name in policy.cache_budgets:
                if size > policy.cache_budgets[name]:
                    candidates.append((entry, size, _identity(entry)))
                else:
                    preserved.append(name)
            elif name in protected or name.startswith((".", "_")):
                preserved.append(name)
            elif (
                entry.is_dir()
                and (entry / name).is_dir()
                and not (entry / name).is_symlink()
            ):
                # Git metadata is expected for disposable CI checkouts. This
                # rule never applies to arbitrary directories or registry clones.
                candidates.append((entry, size, _identity(entry)))
            else:
                preserved.append(name)
        removed: list[str] = []
        failure = ""
        root_identity = (device, os.fstat(work_fd).st_ino)
        for entry, _size_before, identity in candidates:
            if request.dry_run:
                continue
            try:
                if _identity(work) != root_identity:
                    raise ValueError("work root changed after planning")
                info = os.stat(entry.name, dir_fd=work_fd, follow_symlinks=False)
                if (info.st_dev, info.st_ino) != identity or stat.S_ISLNK(info.st_mode):
                    raise ValueError(f"cleanup candidate changed: {entry.name}")
                if stat.S_ISDIR(info.st_mode):
                    shutil.rmtree(entry.name, dir_fd=work_fd)
                else:
                    os.unlink(entry.name, dir_fd=work_fd)
            except (OSError, ValueError) as exc:
                failure = str(exc)
                break
            removed.append(entry.name)
        if not failure and request.phase == "job_started" and not request.dry_run:
            try:
                self._prepare_workspace(work_fd, Path(request.workspace).parts[-1])
            except OSError as exc:
                failure = str(exc)
        after = None
        try:
            if _identity(work) != root_identity:
                raise ValueError("work root changed before readback")
            after = sum(
                _size(entry, device)
                for entry in work.iterdir()
                if not entry.is_symlink()
            )
        except (OSError, ValueError) as exc:
            failure = failure or str(exc)
        return ModelRunnerRetentionResult(
            status="failed" if failure else "completed",
            dry_run=request.dry_run,
            reason=failure,
            removed=tuple(removed),
            preserved=tuple(preserved),
            planned=tuple(entry.name for entry, _, _ in candidates),
            bytes_before=before,
            bytes_after=after,
            bytes_removed=None if after is None else before - after,
        )

    @staticmethod
    def _prepare_workspace(work_fd: int, repository: str) -> None:
        """Recreate through directory descriptors, never an absolute path alias."""
        with suppress(FileExistsError):
            os.mkdir(repository, dir_fd=work_fd)
        repo_fd = os.open(
            repository, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=work_fd
        )
        try:
            try:
                os.mkdir(repository, dir_fd=repo_fd)
            except FileExistsError:
                info = os.stat(repository, dir_fd=repo_fd, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode):
                    raise OSError("incoming workspace became a non-directory") from None
        finally:
            os.close(repo_fd)
