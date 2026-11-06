# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The four seams of the second copy: the source, the target store, the restore drill, the report."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol

from omnimarket.nodes.node_work_ledger_copy_effect.models import (
    ModelCopyResult,
    ModelFileFacts,
    ModelRowFacts,
)


class CopyStepError(Exception):
    """A seam could not do its step. ``step`` is ``dump``, ``transfer`` or ``drill``."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(message)
        self.step = step


class ProtocolCopySource(Protocol):
    def cutoff(self) -> datetime:
        """The database's own clock now; rows projected at or before it are the copy's rows."""

    def row_facts(self, cutoff: datetime) -> ModelRowFacts: ...

    def dump(self, workdir: Path) -> Path:
        """Write the projection tables to one dump file under ``workdir``."""

    def snapshot_file(self, workdir: Path) -> tuple[Path, ModelFileFacts]:
        """Copy the live ledger file's current bytes under ``workdir``; return the copy and its facts."""

    def archive_dir(self) -> Path: ...


class ProtocolCopyStore(Protocol):
    def free_bytes(self) -> int: ...

    def put(self, run_id: str, files: Sequence[Path], archive_dir: Path) -> int:
        """Place the files and the archive rolls on the target; return the bytes written."""

    def digests(self, run_id: str) -> dict[str, str]:
        """The sha256 of each placed file, read on the target, keyed by file name."""

    def prune(self, keep: int) -> tuple[str, ...]: ...


class ProtocolCopyDrill(Protocol):
    def restore(
        self, run_id: str, cutoff: datetime
    ) -> tuple[ModelRowFacts, ModelFileFacts]:
        """Restore the placed copy into a throwaway database; read back its rows and file."""


class ProtocolCopyReporter(Protocol):
    def report(self, result: ModelCopyResult) -> None: ...
