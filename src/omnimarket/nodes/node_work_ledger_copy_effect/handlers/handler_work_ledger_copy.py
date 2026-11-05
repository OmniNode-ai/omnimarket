# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One run of the interim second copy: dump, place, read back, restore on a scratch database.

A run is verified only when four reads agree: the digest of every placed file read on the target
equals the digest taken before it left, the restored projection holds the same number of rows no
later than the cutoff as the source did, its last row is the same row, and the placed ledger file
has the same size, line count and last line as the copy taken from the source. Any other end is a
typed outcome and nothing is retried here.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from omnimarket.nodes.node_work_ledger_copy_effect.models import (
    EnumCopyOutcome,
    ModelCopyRequest,
    ModelCopyResult,
)
from omnimarket.nodes.node_work_ledger_copy_effect.protocols import (
    CopyStepError,
    ProtocolCopyDrill,
    ProtocolCopyReporter,
    ProtocolCopySource,
    ProtocolCopyStore,
)

_CHUNK = 1 << 20


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


class HandlerWorkLedgerCopy:
    def __init__(
        self,
        *,
        source: ProtocolCopySource,
        store: ProtocolCopyStore,
        drill: ProtocolCopyDrill,
        reporter: ProtocolCopyReporter,
        keep: int,
        min_free_bytes: int,
    ) -> None:
        self._source = source
        self._store = store
        self._drill = drill
        self._reporter = reporter
        self._keep = keep
        self._min_free = min_free_bytes

    def handle(self, request: ModelCopyRequest) -> ModelCopyResult:
        result = self._run(request)
        self._reporter.report(result)
        return result

    def _run(self, request: ModelCopyRequest) -> ModelCopyResult:
        run_id = request.run_id
        try:
            free = self._store.free_bytes()
        except CopyStepError as exc:
            return ModelCopyResult(
                run_id=run_id,
                outcome=EnumCopyOutcome.TRANSFER_MISMATCH,
                detail=f"free space on the target could not be read: {exc}",
            )
        if free < self._min_free:
            return ModelCopyResult(
                run_id=run_id,
                outcome=EnumCopyOutcome.REFUSED_LOW_SPACE,
                detail=f"target has {free} bytes free, the floor is {self._min_free}",
                target_free_bytes=free,
            )
        with tempfile.TemporaryDirectory(prefix=f"ledger-copy-{run_id}-") as tmp:
            workdir = Path(tmp)
            try:
                cutoff = self._source.cutoff()
                source_rows = self._source.row_facts(cutoff)
                dump = self._source.dump(workdir)
                ledger, source_file = self._source.snapshot_file(workdir)
            except CopyStepError as exc:
                return ModelCopyResult(
                    run_id=run_id,
                    outcome=EnumCopyOutcome.DUMP_FAILED,
                    detail=str(exc),
                    target_free_bytes=free,
                )
            files = (dump, ledger)
            before = {path.name: sha256_file(path) for path in files}
            try:
                written = self._store.put(run_id, files, self._source.archive_dir())
                after = self._store.digests(run_id)
            except CopyStepError as exc:
                return ModelCopyResult(
                    run_id=run_id,
                    outcome=EnumCopyOutcome.TRANSFER_MISMATCH,
                    detail=str(exc),
                    source_rows=source_rows,
                    source_file=source_file,
                    target_free_bytes=free,
                )
            if after != before:
                differing = sorted(
                    name for name in before if after.get(name) != before[name]
                )
                return ModelCopyResult(
                    run_id=run_id,
                    outcome=EnumCopyOutcome.TRANSFER_MISMATCH,
                    detail=f"digest read on the target differs for {differing}",
                    source_rows=source_rows,
                    source_file=source_file,
                    copy_bytes=written,
                    target_free_bytes=free,
                )
        try:
            restored_rows, restored_file = self._drill.restore(run_id, cutoff)
        except CopyStepError as exc:
            return ModelCopyResult(
                run_id=run_id,
                outcome=EnumCopyOutcome.DRILL_FAILED,
                detail=str(exc),
                source_rows=source_rows,
                source_file=source_file,
                copy_bytes=written,
                target_free_bytes=free,
            )
        mismatches = []
        if restored_rows.row_count != source_rows.row_count:
            mismatches.append(
                f"row count {restored_rows.row_count} against {source_rows.row_count}"
            )
        if restored_rows.last_row_id != source_rows.last_row_id:
            mismatches.append("last row differs")
        if restored_file != source_file:
            mismatches.append("ledger file size, line count or last line differs")
        if mismatches:
            return ModelCopyResult(
                run_id=run_id,
                outcome=EnumCopyOutcome.DRILL_MISMATCH,
                detail="; ".join(mismatches),
                source_rows=source_rows,
                restored_rows=restored_rows,
                source_file=source_file,
                restored_file=restored_file,
                copy_bytes=written,
                target_free_bytes=free,
            )
        pruned = self._store.prune(self._keep)
        return ModelCopyResult(
            run_id=run_id,
            outcome=EnumCopyOutcome.COPIED_VERIFIED,
            detail=(
                f"{restored_rows.row_count} rows and {restored_file.line_count} file lines "
                "restored on a scratch database and matched"
            ),
            source_rows=source_rows,
            restored_rows=restored_rows,
            source_file=source_file,
            restored_file=restored_file,
            copy_bytes=written,
            target_free_bytes=free,
            pruned=pruned,
        )
