# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory seams of the second copy, with named injected breaks."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_work_ledger_copy_effect.models import (
    ModelCopyRequest,
    ModelCopyResult,
    ModelFileFacts,
    ModelRowFacts,
)
from omnimarket.nodes.node_work_ledger_copy_effect.protocols import CopyStepError

CUTOFF = datetime(2026, 11, 5, 18, 0, 0, tzinfo=UTC)
ROWS = ModelRowFacts(row_count=100, last_row_id="r100", state_count=7)
LEDGER = b"row one\nrow two\nlast row\n"


def request() -> ModelCopyRequest:
    return ModelCopyRequest(run_id="20261105T180000Z", started_at=CUTOFF)


def facts_of(data: bytes) -> ModelFileFacts:
    lines = data.split(b"\n")[:-1]
    return ModelFileFacts(
        size_bytes=len(data),
        line_count=len(lines),
        last_line_sha256=hashlib.sha256(lines[-1]).hexdigest(),
    )


class FakeSource:
    def __init__(self, *, fail_dump: bool = False) -> None:
        self.fail_dump = fail_dump
        self.dumped = 0

    def cutoff(self) -> datetime:
        return CUTOFF

    def row_facts(self, cutoff: datetime) -> ModelRowFacts:
        return ROWS

    def dump(self, workdir: Path) -> Path:
        if self.fail_dump:
            raise CopyStepError("dump", "pg_dump exit 1: connection refused")
        self.dumped += 1
        path = workdir / "projection.dump"
        path.write_bytes(b"dump-bytes")
        return path

    def snapshot_file(self, workdir: Path) -> tuple[Path, ModelFileFacts]:
        path = workdir / "ledger.md"
        path.write_bytes(LEDGER)
        return path, facts_of(LEDGER)

    def archive_dir(self) -> Path:
        return Path("/nonexistent-archive")


class FakeStore:
    """Holds what was put. ``corrupt`` flips a byte the digest read must notice."""

    def __init__(self, *, free: int = 10**12, corrupt: bool = False) -> None:
        self.free = free
        self.corrupt = corrupt
        self.placed: dict[str, dict[str, bytes]] = {}
        self.pruned_keep: int | None = None

    def free_bytes(self) -> int:
        return self.free

    def put(self, run_id: str, files: Sequence[Path], archive_dir: Path) -> int:
        self.placed[run_id] = {path.name: path.read_bytes() for path in files}
        if self.corrupt:
            self.placed[run_id]["projection.dump"] = b"dump-bytez"
        return sum(len(data) for data in self.placed[run_id].values())

    def digests(self, run_id: str) -> dict[str, str]:
        return {
            name: hashlib.sha256(data).hexdigest()
            for name, data in self.placed[run_id].items()
        }

    def prune(self, keep: int) -> tuple[str, ...]:
        self.pruned_keep = keep
        return ("20261105T170000Z",)


class FakeDrill:
    """Restores what the store holds. Injected breaks: ``lose_last_row``, ``crash``, ``drop_file_tail``."""

    def __init__(
        self,
        store: FakeStore,
        *,
        lose_last_row: bool = False,
        crash: bool = False,
        drop_file_tail: bool = False,
    ) -> None:
        self.store = store
        self.lose_last_row = lose_last_row
        self.crash = crash
        self.drop_file_tail = drop_file_tail

    def restore(
        self, run_id: str, cutoff: datetime
    ) -> tuple[ModelRowFacts, ModelFileFacts]:
        if self.crash:
            raise CopyStepError("drill", "pg_restore exit 1: relation already exists")
        rows = (
            ModelRowFacts(row_count=99, last_row_id="r99", state_count=7)
            if self.lose_last_row
            else ROWS
        )
        data = self.store.placed[run_id]["ledger.md"]
        if self.drop_file_tail:
            data = data[: data.rindex(b"last row")]
        return rows, facts_of(data)


class FakeReporter:
    def __init__(self) -> None:
        self.reports: list[ModelCopyResult] = []

    def report(self, result: ModelCopyResult) -> None:
        self.reports.append(result)
