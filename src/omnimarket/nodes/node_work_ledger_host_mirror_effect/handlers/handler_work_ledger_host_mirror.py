# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerWorkLedgerHostMirror -- a lab host's copy of the rolling work ledger (OMN-25537).

The ledger of record is on the operator host. A lab host's registry clone holds the ledger file as it
stood when the file last went through git, plus the rows the host's own hooks appended since, so on
2026-11-18 the newest canonical row on each of four lab hosts was 35 days old and a ruling written
hours earlier read as absent. The hourly clone fast-forward (the earlier timer) moves tracked files
and cannot carry rows that never reach git.

One tick reads the rows the host has not seen from the work ledger read node over the bus, oldest
first, and appends them to the host's mirror file; the host timer runs it every 120 s. A tick resumes
from a watermark, re-reads ``overlap_seconds`` behind it, and drops a row it already holds by row id.
The mirror's byte length is recorded in the state after each append, so a tick that died between the
append and the state write truncates back to what the state recorded and never leaves a duplicate.
A failed read changes nothing.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.nodes.node_work_ledger_host_mirror_effect.models.model_work_ledger_host_mirror import (
    EnumHostMirrorStatus,
    ModelHostMirrorRequest,
    ModelHostMirrorResult,
)
from omnimarket.nodes.node_work_ledger_host_mirror_effect.protocols import (
    LedgerReadError,
    ModelLedgerRow,
    ProtocolLedgerRowsReader,
)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    held = path.with_suffix(path.suffix + ".tmp")
    held.write_text(text, encoding="utf-8")
    held.replace(path)


class HandlerWorkLedgerHostMirror:
    def __init__(self, reader: ProtocolLedgerRowsReader) -> None:
        self._reader = reader

    def handle(self, request: ModelHostMirrorRequest) -> ModelHostMirrorResult:
        now = request.now or datetime.now(UTC)
        watermark, seen, recorded_bytes = self._load_state(request.state_path)
        reset = False
        mirror = request.mirror_path
        size = mirror.stat().st_size if mirror.exists() else 0
        if watermark is not None and size < recorded_bytes:
            # The mirror lost bytes the state counted: start over from the window rather than trust it.
            watermark, seen, recorded_bytes, reset = None, {}, 0, True
        since = (
            now - timedelta(seconds=request.window_seconds)
            if watermark is None
            else watermark - timedelta(seconds=request.overlap_seconds)
        )

        fresh: list[ModelLedgerRow] = []
        cursor: str | None = None
        record_newest: datetime | None = None
        pages = 0
        partial = False
        try:
            while True:
                if pages == request.max_pages:
                    partial = True
                    break
                page = self._reader.read_rows(
                    since=since, cursor=cursor, page_size=request.page_size
                )
                pages += 1
                record_newest = page.record_newest_row_ts or record_newest
                fresh.extend(row for row in page.rows if row.row_id not in seen)
                cursor = page.next_cursor
                if cursor is None:
                    break
        except LedgerReadError as exc:
            return ModelHostMirrorResult(
                status=EnumHostMirrorStatus.REFUSED,
                rows_appended=0,
                pages=pages,
                mirror_path=str(mirror),
                mirror_newest_row_ts=watermark,
                reset=reset,
                detail=str(exc),
            )

        fresh.sort(key=lambda row: row.row_ts)
        new_watermark = max([row.row_ts for row in fresh], default=watermark)
        if new_watermark is not None:
            floor = new_watermark - timedelta(seconds=request.overlap_seconds)
            seen = {rid: ts for rid, ts in seen.items() if ts >= floor}
            seen.update(
                {row.row_id: row.row_ts for row in fresh if row.row_ts >= floor}
            )
        size_after = self._append(mirror, fresh, recorded_bytes, reset)
        self._store_state(request.state_path, new_watermark, seen, size_after)

        lag = (
            int((record_newest - new_watermark).total_seconds())
            if record_newest is not None and new_watermark is not None
            else None
        )
        return ModelHostMirrorResult(
            status=(
                EnumHostMirrorStatus.PARTIAL
                if partial
                else EnumHostMirrorStatus.MIRRORED
            ),
            rows_appended=len(fresh),
            pages=pages,
            mirror_path=str(mirror),
            mirror_newest_row_ts=new_watermark,
            record_newest_row_ts=record_newest,
            lag_seconds=lag,
            reset=reset,
        )

    @staticmethod
    def _append(
        mirror: Path, rows: list[ModelLedgerRow], recorded_bytes: int, reset: bool
    ) -> int:
        mirror.parent.mkdir(parents=True, exist_ok=True)
        with mirror.open("ab" if not reset else "wb") as held:
            if not reset:
                # Cut back to what the state recorded: bytes past it are an append whose state write never landed.
                held.truncate(recorded_bytes)
                held.seek(recorded_bytes)
            for row in rows:
                held.write(row.raw_row.rstrip("\n").encode("utf-8") + b"\n")
            held.flush()
            os.fsync(held.fileno())
            return held.tell()

    @staticmethod
    def _load_state(path: Path) -> tuple[datetime | None, dict[str, datetime], int]:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            return (
                datetime.fromisoformat(state["watermark"]),
                {rid: datetime.fromisoformat(ts) for rid, ts in state["seen"].items()},
                int(state["mirror_bytes"]),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None, {}, 0

    @staticmethod
    def _store_state(
        path: Path,
        watermark: datetime | None,
        seen: dict[str, datetime],
        mirror_bytes: int,
    ) -> None:
        if watermark is None:
            return
        _atomic_write(
            path,
            json.dumps(
                {
                    "watermark": watermark.isoformat(),
                    "seen": {rid: ts.isoformat() for rid, ts in seen.items()},
                    "mirror_bytes": mirror_bytes,
                }
            ),
        )
