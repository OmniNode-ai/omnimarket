# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Conservative host hygiene using a deployment census.

Sweep stale temporary entries owned by this user only when no process holds them.
Preserve session entries, declared lane containers, running containers and compose
projects with running containers. Prune unused images and cap the build cache;
prune anonymous volumes only on engines that support anonymous-only pruning.
An unreadable census preserves every container while the prunes still run.
"""

from __future__ import annotations

import contextlib
import fnmatch
import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ..models.model_lab_disk_hygiene_deployment import ModelLabDiskHygieneDeployment
from ..models.model_lab_disk_hygiene_request import ModelLabDiskHygieneRequest
from ..models.model_lab_disk_hygiene_result import (
    ModelLabDiskHygieneResult,
    ModelLabDiskHygieneStep,
)

OVERLAY_ENV = "OMNIMARKET_LAB_DISK_HYGIENE_OVERLAY"


class LabDiskHygieneConfigurationError(ValueError):
    """Missing or invalid deployment configuration."""


def load_deployment_overlay() -> ModelLabDiskHygieneDeployment | None:
    """Read the explicit pointer, otherwise the first existing overlay in root order."""
    if OVERLAY_ENV in os.environ:
        pointer = os.environ[OVERLAY_ENV]
        if not pointer:
            raise LabDiskHygieneConfigurationError(
                f"{OVERLAY_ENV}: empty overlay pointer"
            )
        path = Path(pointer)
        source = OVERLAY_ENV
    else:
        path = None
        source = "ONEX_SKILL_OVERLAY_ROOTS"
        for root in os.environ.get(source, "").split(os.pathsep):
            if not root:
                continue
            candidate = Path(root) / "node_lab_disk_hygiene_effect" / "overlay.yaml"
            if candidate.exists():
                path = candidate
                break
    if path is None:
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("overlay must be a mapping")
        if set(raw) != {"census_path"}:
            raise ValueError("overlay must contain exactly census_path")
        return ModelLabDiskHygieneDeployment(census_path=raw["census_path"])
    except (OSError, UnicodeError, yaml.YAMLError, ValueError, TypeError) as exc:
        raise LabDiskHygieneConfigurationError(
            f"{source}: invalid deployment overlay ({type(exc).__name__})"
        ) from None


STOPPED_STATES = frozenset({"exited", "created", "dead"})
PS_FORMAT = '{{.ID}}\t{{.Names}}\t{{.State}}\t{{.Label "com.docker.compose.project"}}'
INSPECT_FORMAT = "{{.Id}}\t{{.State.FinishedAt}}"
DOCKER_TIMEOUT_S = 1800
# Session and service entries that live in a temp dir for as long as their owner does.
TMP_RESERVED = (
    "tmux-*",
    "ssh-*",
    ".X*",
    ".ICE-unix",
    ".font-unix",
    "pulse-*",
    "systemd-private-*",
    "snap-private-tmp",
    "*.sock",
    "*.pid",
    "*.lock",
)
# Docker 23 made `docker volume prune` anonymous-only; before it, unused named volumes went too.
ANONYMOUS_ONLY_VOLUME_PRUNE_MAJOR = 23

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _subprocess_run(argv: Sequence[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    timeout = int(kw.get("timeout", DOCKER_TIMEOUT_S))
    try:
        return subprocess.run(
            list(argv), capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(list(argv), 124, "", f"timed out: {exc}")
    except OSError as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def declared_lane_objects(census: Path) -> tuple[set[str], set[str]]:
    """(compose projects, container names) every declared lane owns, on any host."""
    try:
        data = yaml.safe_load(census.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"{census}: {exc}") from exc
    lanes = data.get("lanes") if isinstance(data, dict) else None
    if not isinstance(lanes, dict):
        raise ValueError(f"{census}: no lanes mapping")
    projects: set[str] = set()
    names: set[str] = set()
    for lane in lanes.values():
        if not isinstance(lane, dict):
            continue
        project = lane.get("compose_project")
        if isinstance(project, str) and project:
            projects.add(project)
        for value in lane.values():
            if not isinstance(value, list):
                continue
            for item in value:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    names.add(item["name"])
    return projects, names


def _tail(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:300] if lines else ""


def _finished_at(raw: str) -> datetime | None:
    """A Docker RFC 3339 time with nanoseconds, or None for the zero time or garbage."""
    text = raw.strip()
    if not text or text.startswith("0001-01-01"):
        return None
    text = text.replace("Z", "+00:00")
    if "." in text:
        head, rest = text.split(".", 1)
        count = len(rest) - len(rest.lstrip("0123456789"))
        digits, zone = rest[:count], rest[count:]
        text = f"{head}.{digits[:6]}{zone}"
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _lane_named(name: str, declared: set[str], names: set[str]) -> bool:
    """The census names the container, or it carries a declared project's name prefix."""
    return name in names or any(name.startswith(f"{project}-") for project in declared)


def mapped_paths(maps: str) -> set[str]:
    """The file paths of a /proc/<pid>/maps text. An anonymous mapping has five fields and
    padding spaces after the fifth, so a line is split, never indexed by its space count."""
    paths: set[str] = set()
    for line in maps.splitlines():
        fields = line.split(None, 5)
        if len(fields) == 6 and fields[5].startswith("/"):
            paths.add(fields[5])
    return paths


def _held_paths() -> set[str] | None:
    """Every path a readable process works in, holds open or maps; None without /proc."""
    proc = Path("/proc")
    if not (proc / "self").is_dir():
        return None
    held: set[str] = set()
    for pid in proc.iterdir():
        if not pid.name.isdigit():
            continue
        for link in (pid / "cwd", pid / "root"):
            with contextlib.suppress(OSError):
                held.add(os.readlink(link))
        try:
            fds = list((pid / "fd").iterdir())
        except OSError:
            fds = []
        for fd in fds:
            with contextlib.suppress(OSError):
                held.add(os.readlink(fd))
        try:
            maps = (pid / "maps").read_text(encoding="utf-8", errors="replace")
        except OSError:
            maps = ""
        held.update(mapped_paths(maps))
    return held


def _stale_tmp_entry(entry: Path, cutoff: float, held: set[str]) -> bool:
    """True for an entry this user owns, of no reserved name, untouched since cutoff, unheld."""
    name = entry.name
    if any(fnmatch.fnmatch(name, pattern) for pattern in TMP_RESERVED):
        return False
    try:
        st = entry.lstat()
    except OSError:
        return False
    if st.st_uid != os.getuid() or entry.is_symlink():
        return False
    prefix = str(entry)
    if any(path == prefix or path.startswith(prefix + "/") for path in held):
        return False
    if st.st_mtime >= cutoff:
        return False
    if entry.is_dir():
        for dirpath, dirnames, filenames in os.walk(entry):
            for child in [*dirnames, *filenames]:
                try:
                    if os.lstat(os.path.join(dirpath, child)).st_mtime >= cutoff:
                        return False
                except OSError:
                    return False
    return True


def _tree_bytes(entry: Path) -> int:
    if not entry.is_dir() or entry.is_symlink():
        try:
            return entry.lstat().st_size
        except OSError:
            return 0
    total = 0
    for dirpath, _, filenames in os.walk(entry):
        for name in filenames:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(dirpath, name)).st_size
    return total


def _major(version: str) -> int:
    head = version.strip().split(".", 1)[0]
    return int(head) if head.isdigit() else 0


class HandlerLabDiskHygiene:
    """Run one Docker hygiene pass and report what it removed and freed."""

    def __init__(
        self,
        run: Runner | None = None,
        disk_free: Callable[[Path], int] | None = None,
        which: Callable[[str], str | None] | None = None,
        now: Callable[[], datetime] | None = None,
        held_paths: Callable[[], set[str] | None] | None = None,
        overlay: ModelLabDiskHygieneDeployment | None = None,
    ) -> None:
        self._overlay = overlay
        self._run: Runner = run or _subprocess_run
        self._disk_free = disk_free or _free_bytes
        self._which = which or shutil.which
        self._now = now or (lambda: datetime.now(UTC))
        self._held_paths = held_paths or _held_paths

    def handle(self, request: ModelLabDiskHygieneRequest) -> ModelLabDiskHygieneResult:
        census = request.census
        if census is None:
            deployment = self._overlay or load_deployment_overlay()
            if deployment is not None:
                census = request.omni_home / deployment.census_path
        before = self._disk_free(request.omni_home)
        errors: list[str] = []
        tmp_removed, tmp_bytes = self._stale_tmp(request, errors)
        if self._which("docker") is None:
            return ModelLabDiskHygieneResult(
                docker="absent",
                free_bytes_before=before,
                free_bytes_after=self._disk_free(request.omni_home),
                removed_tmp=tuple(tmp_removed),
                removed_tmp_bytes=tmp_bytes,
                errors=tuple(errors),
            )
        version = self._run(["docker", "version", "--format", "{{.Server.Version}}"])
        if version.returncode != 0:
            errors.append(
                f"docker version exit {version.returncode}: {_tail(version.stderr)}"
            )
            return ModelLabDiskHygieneResult(
                docker="unreachable",
                free_bytes_before=before,
                free_bytes_after=self._disk_free(request.omni_home),
                removed_tmp=tuple(tmp_removed),
                removed_tmp_bytes=tmp_bytes,
                errors=tuple(errors),
            )
        steps: list[ModelLabDiskHygieneStep] = []
        census_ok = True
        try:
            if census is None:
                raise ValueError(
                    "census not configured: no request census and no overlay census_path; no container removed"
                )
            declared, names = declared_lane_objects(census)
        except ValueError as exc:
            census_ok, declared, names = False, set(), set()
            errors.append(
                str(exc)
                if census is None
                else f"census unreadable, no container removed: {exc}"[:400]
            )
        removed, kept, running = self._containers(
            request, census_ok, declared, names, steps, errors
        )
        if not request.dry_run:
            self._prunes(request, _major(version.stdout), steps, errors)
        return ModelLabDiskHygieneResult(
            docker="dry-run" if request.dry_run else "ran",
            free_bytes_before=before,
            free_bytes_after=self._disk_free(request.omni_home),
            protected_projects=tuple(sorted(declared | running)),
            removed_containers=tuple(removed),
            kept_containers=tuple(kept),
            removed_tmp=tuple(tmp_removed),
            removed_tmp_bytes=tmp_bytes,
            steps=tuple(steps),
            errors=tuple(errors),
        )

    def _stale_tmp(
        self, request: ModelLabDiskHygieneRequest, errors: list[str]
    ) -> tuple[list[str], int]:
        """Remove top-level temp entries this user owns that nothing touched or holds."""
        if not request.tmp_dirs:
            return [], 0
        held = self._held_paths()
        if held is None:
            errors.append(
                "stale tmp skipped: process working paths unreadable (no /proc)"
            )
            return [], 0
        cutoff = self._now().timestamp() - request.tmp_stale_hours * 3600
        removed: list[str] = []
        freed = 0
        for root in request.tmp_dirs:
            try:
                entries = sorted(root.iterdir())
            except OSError as exc:
                errors.append(f"stale tmp: {root}: {exc}"[:300])
                continue
            for entry in entries:
                if not _stale_tmp_entry(entry, cutoff, held):
                    continue
                size = _tree_bytes(entry)
                if not request.dry_run:
                    try:
                        if entry.is_dir() and not entry.is_symlink():
                            shutil.rmtree(entry)
                        else:
                            entry.unlink()
                    except OSError as exc:
                        errors.append(f"stale tmp: {entry}: {exc}"[:300])
                        continue
                removed.append(entry.name)
                freed += size
        return removed, freed

    def _step(
        self,
        name: str,
        argv: list[str],
        steps: list[ModelLabDiskHygieneStep],
        errors: list[str],
    ) -> subprocess.CompletedProcess[str]:
        proc = self._run(argv)
        steps.append(
            ModelLabDiskHygieneStep(
                step=name,
                argv=tuple(argv),
                exit_code=proc.returncode,
                detail=_tail(proc.stdout or proc.stderr),
            )
        )
        if proc.returncode != 0:
            errors.append(f"{name} exit {proc.returncode}: {_tail(proc.stderr)}")
        return proc

    def _containers(
        self,
        request: ModelLabDiskHygieneRequest,
        census_ok: bool,
        declared: set[str],
        names: set[str],
        steps: list[ModelLabDiskHygieneStep],
        errors: list[str],
    ) -> tuple[list[str], list[str], set[str]]:
        listed = self._run(["docker", "ps", "-a", "--no-trunc", "--format", PS_FORMAT])
        if listed.returncode != 0:
            errors.append(f"docker ps exit {listed.returncode}: {_tail(listed.stderr)}")
            return [], [], set()
        rows = [line.split("\t") for line in listed.stdout.splitlines() if line.strip()]
        rows = [[*r, "", "", "", ""][:4] for r in rows]
        running = {r[3] for r in rows if r[2] == "running" and r[3]}
        removed: list[str] = []
        kept: list[str] = []
        candidates: dict[str, str] = {}
        for cid, name, state, project in rows:
            if state not in STOPPED_STATES:
                continue
            if (
                not census_ok
                or project in declared
                or project in running
                or _lane_named(name, declared, names)
            ):
                kept.append(name)
            else:
                candidates[cid] = name
        if not candidates:
            return removed, kept, running
        ages = self._run(["docker", "inspect", "--format", INSPECT_FORMAT, *candidates])
        finished = {}
        for line in ages.stdout.splitlines():
            cid, _, stamp = line.partition("\t")
            finished[cid.strip()] = _finished_at(stamp)
        bar = request.exited_container_hours * 3600
        doomed: list[str] = []
        for cid, name in candidates.items():
            when = finished.get(cid)
            if when is not None and (self._now() - when).total_seconds() < bar:
                kept.append(name)
            else:
                doomed.append(cid)
                removed.append(name)
        if doomed and not request.dry_run:
            self._step("containers", ["docker", "rm", *doomed], steps, errors)
        return removed, kept, running

    def _prunes(
        self,
        request: ModelLabDiskHygieneRequest,
        major: int,
        steps: list[ModelLabDiskHygieneStep],
        errors: list[str],
    ) -> None:
        self._step(
            "images",
            [
                "docker",
                "image",
                "prune",
                "-af",
                "--filter",
                f"until={request.image_unused_hours}h",
            ],
            steps,
            errors,
        )
        cap = f"{request.build_cache_max_gb:g}gb"
        builder = ["docker", "builder", "prune", "-af"]
        proc = self._run([*builder, "--max-used-space", cap])
        if proc.returncode != 0 and "unknown flag" in proc.stderr:
            self._step("build_cache", [*builder, "--keep-storage", cap], steps, errors)
        else:
            steps.append(
                ModelLabDiskHygieneStep(
                    step="build_cache",
                    argv=(*builder, "--max-used-space", cap),
                    exit_code=proc.returncode,
                    detail=_tail(proc.stdout or proc.stderr),
                )
            )
            if proc.returncode != 0:
                errors.append(
                    f"build_cache exit {proc.returncode}: {_tail(proc.stderr)}"
                )
        if (
            request.prune_anonymous_volumes
            and major >= ANONYMOUS_ONLY_VOLUME_PRUNE_MAJOR
        ):
            self._step("volumes", ["docker", "volume", "prune", "-f"], steps, errors)
