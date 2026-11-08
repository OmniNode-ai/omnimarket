# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real seams of the snapshot job and the restore drill, and their in-memory twins.

Every command is an argument vector from the overlay, run without a shell through an injected
runner. The drill issues no command that names the source host except the reachability probe,
which only tries to connect.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from omnimarket.nodes.node_work_ledger_copy_effect.adapters.adapters_real import (
    Runner,
    expand_home,
    run_command,
)
from omnimarket.nodes.node_work_ledger_copy_effect.models import (
    ModelDrillResult,
    ModelLedgerRow,
    ModelSnapshotBundle,
    ModelSnapshotManifest,
    ModelSnapshotOverlay,
)
from omnimarket.nodes.node_work_ledger_copy_effect.protocols import CopyStepError

_RUN_DIR: Final = re.compile(r"^[A-Za-z0-9T_-]+$")
_MANIFEST: Final = "manifest.json"
_TOPIC: Final = "topic.jsonl"
_PROJECTION: Final = "projection.jsonl"


def parse_rows(text: str) -> tuple[ModelLedgerRow, ...]:
    rows: list[ModelLedgerRow] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(ModelLedgerRow.model_validate(json.loads(line)))
        except ValueError as exc:
            raise CopyStepError(
                "dump", f"line {number} is not a ledger row: {exc}"
            ) from None
    return tuple(sorted(rows, key=lambda row: row.seq))


class CommandRowSource:
    """Rows printed one JSON object per line by an overlay-declared command."""

    def __init__(
        self,
        argv: Sequence[str],
        timeout: int,
        run: Runner = run_command,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._argv = tuple(expand_home(part) for part in argv)
        self._timeout = timeout
        self._run = run
        self._environ = environ

    def rows(self) -> tuple[ModelLedgerRow, ...]:
        done = self._run(list(self._argv), self._environ, self._timeout)
        if done.returncode:
            first = next(
                (x.strip() for x in done.stderr.splitlines() if x.strip()), "no output"
            )
            raise CopyStepError(
                "dump", f"export exited {done.returncode}: {first[:300]}"
            )
        return parse_rows(done.stdout)


class CommandSourceProbe:
    """Whether the source host answers from this host: the overlay's probe argv, exit 0 or 1."""

    def __init__(
        self,
        argv: Sequence[str],
        timeout: int,
        run: Runner = run_command,
    ) -> None:
        self._argv = tuple(argv)
        self._timeout = timeout
        self._run = run

    def reachable(self, host: str, port: int) -> bool:
        argv = [
            part.replace("{host}", host).replace("{port}", str(port))
            for part in self._argv
        ]
        done = self._run(argv, None, self._timeout)
        if done.returncode == 0:
            return True
        if done.returncode == 1:
            return False
        raise CopyStepError(
            "probe",
            f"the probe exited {done.returncode}, neither reachable nor unreachable",
        )


class DirectorySnapshotStore:
    """Snapshots as ``<directory>/<run_id>/{manifest.json,topic.jsonl,projection.jsonl}``.

    A snapshot is written beside its final name and renamed into place, so a reader sees all of it
    or none of it. ``read`` returns the files as they lie; the drill does the checking.
    """

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(expand_home(str(directory)))

    @staticmethod
    def _jsonl(rows: Sequence[ModelLedgerRow]) -> str:
        return "".join(row.model_dump_json() + "\n" for row in rows)

    def write(self, bundle: ModelSnapshotBundle) -> None:
        run_id = bundle.manifest.run_id
        work: Path | None = None
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            work = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=self._dir))
            (work / _MANIFEST).write_text(
                bundle.manifest.model_dump_json(), encoding="utf-8"
            )
            (work / _TOPIC).write_text(self._jsonl(bundle.topic_rows), encoding="utf-8")
            (work / _PROJECTION).write_text(
                self._jsonl(bundle.projection_rows), encoding="utf-8"
            )
            final = self._dir / run_id
            if final.exists():
                shutil.rmtree(work)
                raise CopyStepError("store", f"snapshot {run_id} already exists")
            os.replace(work, final)
        except OSError as exc:
            if work is not None:
                shutil.rmtree(work, ignore_errors=True)
            raise CopyStepError("store", f"{type(exc).__name__}: {exc}") from None

    def read(self, run_id: str) -> ModelSnapshotBundle | None:
        if not _RUN_DIR.match(run_id):
            raise CopyStepError("store", f"{run_id!r} is not a run id")
        path = self._dir / run_id
        if not path.is_dir():
            return None
        try:
            manifest = ModelSnapshotManifest.model_validate_json(
                (path / _MANIFEST).read_text(encoding="utf-8")
            )
            topic = parse_rows((path / _TOPIC).read_text(encoding="utf-8"))
            projection = parse_rows((path / _PROJECTION).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CopyStepError(
                "store", f"snapshot {run_id} unreadable: {exc}"
            ) from None
        return ModelSnapshotBundle(
            manifest=manifest, topic_rows=topic, projection_rows=projection
        )

    def _runs(self) -> list[str]:
        if not self._dir.is_dir():
            return []
        return sorted(
            p.name for p in self._dir.iterdir() if p.is_dir() and _RUN_DIR.match(p.name)
        )

    def newest(self) -> str | None:
        runs = self._runs()
        return runs[-1] if runs else None

    def prune(self, keep: int) -> tuple[str, ...]:
        gone = self._runs()[:-keep] if keep else []
        for name in gone:
            try:
                shutil.rmtree(self._dir / name)
            except OSError as exc:
                raise CopyStepError("store", f"could not prune {name}: {exc}") from None
        return tuple(gone)


class SqliteTableRebuilder:
    """Builds an empty scratch table from topic rows alone, then reads it back in sequence order."""

    def __init__(self, table: str) -> None:
        self._table = table

    def rebuild(
        self, topic_rows: Sequence[ModelLedgerRow]
    ) -> tuple[ModelLedgerRow, ...]:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                f"CREATE TABLE {self._table} (seq INTEGER PRIMARY KEY, row_id TEXT NOT NULL, text TEXT NOT NULL)"
            )
            conn.executemany(
                f"INSERT INTO {self._table} (seq, row_id, text) VALUES (?, ?, ?)",
                [(row.seq, row.row_id, row.text) for row in topic_rows],
            )
            read = conn.execute(
                f"SELECT seq, row_id, text FROM {self._table} ORDER BY seq"
            ).fetchall()
        except sqlite3.Error as exc:
            raise CopyStepError("drill", f"{type(exc).__name__}: {exc}") from None
        finally:
            conn.close()
        return tuple(ModelLedgerRow(seq=s, row_id=r, text=t) for s, r, t in read)


class AppendDrillReporter:
    """Appends the drill's receipt as one STATUS row through the configured ledger append command."""

    def __init__(
        self,
        overlay: ModelSnapshotOverlay,
        timeout: int,
        run: Runner = run_command,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._o = overlay
        self._timeout = timeout
        self._run = run
        self._environ = environ
        self._clock = clock

    def row(self, result: ModelDrillResult) -> str:
        text = re.sub(r"\s*\|\s*|\s+", " ", result.receipt()).strip()[:600]
        return " | ".join(
            [
                self._clock().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "STATUS",
                f"lane={self._o.report_lane}",
                f"ticket={self._o.report_ticket}",
                "src=ledger-restore-drill",
                f"actor={self._o.report_actor}",
                "model=none",
                f"outcome={result.outcome.value}",
                f"drill_host={result.drill_host}",
                text,
            ]
        )

    def report(self, result: ModelDrillResult) -> None:
        done = self._run(
            [*map(expand_home, self._o.report_append_argv), self.row(result)],
            self._environ,
            self._timeout,
        )
        if done.returncode:
            raise CopyStepError("report", f"the ledger append exited {done.returncode}")


# In-memory twins: the default profile, used by every test and by a dry run with no backend.
class MemoryRowSource:
    def __init__(self, rows: Sequence[ModelLedgerRow]) -> None:
        self._rows = tuple(rows)

    def rows(self) -> tuple[ModelLedgerRow, ...]:
        return self._rows


class MemorySnapshotStore:
    def __init__(self) -> None:
        self.bundles: dict[str, ModelSnapshotBundle] = {}

    def write(self, bundle: ModelSnapshotBundle) -> None:
        self.bundles[bundle.manifest.run_id] = bundle

    def read(self, run_id: str) -> ModelSnapshotBundle | None:
        return self.bundles.get(run_id)

    def newest(self) -> str | None:
        return max(self.bundles) if self.bundles else None

    def prune(self, keep: int) -> tuple[str, ...]:
        gone = sorted(self.bundles)[:-keep]
        for name in gone:
            del self.bundles[name]
        return tuple(gone)
