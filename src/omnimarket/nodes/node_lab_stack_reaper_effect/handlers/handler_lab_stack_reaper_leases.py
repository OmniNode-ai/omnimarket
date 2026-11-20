# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Atomic lease records and stable flock identities.

The caller supplies a directory dedicated to one docker daemon. Locks are never unlinked:
replacing a lock inode would let two callers enter the same project's critical section.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from .lab_stack_support import PROJECT_RE, STAMP_RE


@dataclass(frozen=True, slots=True)
class ComposeFile:
    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class Lease:
    lane: str
    claim: str
    compose_files: tuple[ComposeFile, ...] = ()


@dataclass(frozen=True, slots=True)
class LeaseRecord:
    project: str
    generation: str
    leases: tuple[Lease, ...] = ()


def validate_project(project: str) -> None:
    if not PROJECT_RE.fullmatch(project):
        raise ValueError(f"invalid compose project {project!r}")


def validate_claim(claim: str) -> None:
    from datetime import datetime

    if not STAMP_RE.fullmatch(claim):
        raise ValueError("claim must be a CLAIM row stamp YYYY-MM-DDTHH:MM:SSZ")
    datetime.strptime(claim, "%Y-%m-%dT%H:%M:%SZ")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path: Path, data: object) -> None:
    """Replace only the data file, with a synced temporary in the same directory."""
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


class LeaseStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path(self, project: str, suffix: str) -> Path:
        validate_project(project)
        return self.directory / f"{project}.{suffix}"

    @contextmanager
    def lock(self, project: str) -> Iterator[None]:
        with self.path(project, "lock").open("a", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def projects(self) -> list[str]:
        # iterdir, unlike glob, propagates an unreadable directory instead of answering empty.
        return sorted(
            path.stem
            for path in self.directory.iterdir()
            if path.suffix == ".json" and path.name != "last-run.json"
        )

    def read(self, project: str) -> LeaseRecord:
        path = self.path(project, "json")
        if not path.exists():
            return LeaseRecord(project, "")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("project") != project:
            raise ValueError(f"invalid lease record for {project}")
        generation, entries = data.get("generation"), data.get("leases")
        if not isinstance(generation, str) or not isinstance(entries, list):
            raise ValueError(f"invalid generation or leases for {project}")
        leases: list[Lease] = []
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError(f"invalid lease for {project}")
            lane, claim, files = (
                item.get("lane"),
                item.get("claim"),
                item.get("compose_files"),
            )
            if (
                not isinstance(lane, str)
                or not lane.strip()
                or not isinstance(claim, str)
            ):
                raise ValueError(f"invalid lease identity for {project}")
            validate_claim(claim)
            if not isinstance(files, list):
                raise ValueError(f"invalid compose files for {project}")
            recorded: list[ComposeFile] = []
            for file in files:
                if (
                    not isinstance(file, dict)
                    or not isinstance(file.get("path"), str)
                    or not isinstance(file.get("sha256"), str)
                    or not file["path"]
                    or len(file["sha256"]) != 64
                ):
                    raise ValueError(f"invalid compose file for {project}")
                recorded.append(ComposeFile(file["path"], file["sha256"]))
            leases.append(Lease(lane.lower(), claim, tuple(recorded)))
        return LeaseRecord(project, generation, tuple(leases))

    def add(
        self,
        project: str,
        lane: str,
        claim: str,
        generation: str,
        files: tuple[Path, ...],
    ) -> None:
        validate_project(project)
        validate_claim(claim)
        if not lane.strip() or any(c in lane for c in "|\r\n"):
            raise ValueError("a nonempty lane is required")
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock(project):
            if self.path(project, "fence").exists():
                raise ValueError(f"{project}: removal fenced; lease refused")
            # Hash before changing anything, including a generation reset.
            recorded = tuple(ComposeFile(str(p.resolve()), digest(p)) for p in files)
            record = self.read(project)
            leases = list(record.leases) if record.generation == generation else []
            identity = (lane.strip().lower(), claim)
            existing = next((x for x in leases if (x.lane, x.claim) == identity), None)
            if existing is not None:
                if existing.compose_files != recorded:
                    raise ValueError(
                        "an existing lease cannot change its teardown inputs"
                    )
                return
            leases.append(Lease(*identity, recorded))
            atomic_json(
                self.path(project, "json"),
                asdict(LeaseRecord(project, generation, tuple(leases))),
            )

    def clear(self, project: str) -> None:
        """Only called under lock after the daemon reports this project gone."""
        self.path(project, "json").unlink(missing_ok=True)
        self.path(project, "fence").unlink(missing_ok=True)
