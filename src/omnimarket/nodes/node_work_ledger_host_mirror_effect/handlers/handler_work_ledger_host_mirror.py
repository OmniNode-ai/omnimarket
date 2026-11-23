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
A failed read keeps the pages already recorded and appends nothing more.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.nodes.node_work_ledger_host_mirror_effect.models.model_pinned_rulings_mirror import (
    EnumPinnedRulingsStatus,
    ModelPinnedRulingsRequest,
    ModelPinnedRulingsResult,
)
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
            watermark, seen, reset = None, {}, True
        if watermark is None:
            recorded_bytes = 0
        since = (
            now - timedelta(seconds=request.window_seconds)
            if watermark is None
            else watermark - timedelta(seconds=request.overlap_seconds)
        )

        appended = 0
        cursor: str | None = None
        record_newest: datetime | None = None
        pages = 0
        partial = False
        detail = ""
        failed = False
        while True:
            if pages == request.max_pages:
                partial = True
                break
            try:
                page = self._reader.read_rows(
                    since=since, cursor=cursor, page_size=request.page_size
                )
            except LedgerReadError as exc:
                failed, detail = True, str(exc)
                break
            pages += 1
            record_newest = page.record_newest_row_ts or record_newest
            fresh = sorted(
                (row for row in page.rows if row.row_id not in seen),
                key=lambda row: row.row_ts,
            )
            # Each page is appended and recorded before the next is read, so a tick the run limit ends
            # keeps the pages it finished and the next one resumes from them.
            recorded_bytes = self._append(mirror, fresh, recorded_bytes)
            appended += len(fresh)
            if fresh:
                watermark = max(watermark or fresh[-1].row_ts, fresh[-1].row_ts)
                floor = watermark - timedelta(seconds=request.overlap_seconds)
                seen = {rid: ts for rid, ts in seen.items() if ts >= floor}
                seen.update(
                    {row.row_id: row.row_ts for row in fresh if row.row_ts >= floor}
                )
                self._store_state(request.state_path, watermark, seen, recorded_bytes)
            cursor = page.next_cursor
            if cursor is None:
                break

        lag = (
            int((record_newest - watermark).total_seconds())
            if record_newest is not None and watermark is not None
            else None
        )
        if failed:
            status = EnumHostMirrorStatus.REFUSED
        elif partial:
            status = EnumHostMirrorStatus.PARTIAL
        else:
            status = EnumHostMirrorStatus.MIRRORED
        return ModelHostMirrorResult(
            status=status,
            rows_appended=appended,
            pages=pages,
            mirror_path=str(mirror),
            mirror_newest_row_ts=watermark,
            record_newest_row_ts=record_newest,
            lag_seconds=lag,
            reset=reset,
            detail=detail,
        )

    @staticmethod
    def _append(mirror: Path, rows: list[ModelLedgerRow], recorded_bytes: int) -> int:
        mirror.parent.mkdir(parents=True, exist_ok=True)
        with mirror.open("r+b" if mirror.exists() else "wb") as held:
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


class HandlerPinnedRulingsMirror:
    """The rulings a brief cites stay on a host's mirror whatever their age (OMN-26412).

    A lab host's mirror holds the authority rows of the last day and the rulings of the last 30 days, because the bus
    projection answers for recent rows. A brief cites its landing ruling by stamp and lane, and the lane-brief reader
    looks for that row in the file it is given and in the ``archive/`` directory beside it. The standing landing ruling
    is older than 30 days, so a brief built on a lab host stopped at ``lane_brief.py constraints`` exit 3 (read on h202,
    2026-11-22: "no row reads '2026-09-29T06:34:17Z | RULING | lane=orchestrator-83 |'").

    ``handle`` copies each declared ruling, verbatim, from the host's own archive rolls (the registry clone tracks them;
    the projection holds no row that old) into one roll beside the mirror, ``archive/pinned-rulings-split.md``, which the
    brief reader already globs. It rewrites the roll only when its content changes, in stamp order, so two runs write the
    same bytes. A ruling found in no roll is named, not skipped: the caller reports it.
    """

    def handle(self, request: ModelPinnedRulingsRequest) -> ModelPinnedRulingsResult:
        rolls = (
            sorted(request.rolls_directory.glob("*-split.md"))
            if request.rolls_directory.is_dir()
            else []
        )
        found: dict[str, str] = {}
        for roll in rolls:
            with roll.open(encoding="utf-8", errors="replace") as held:
                for line in held:
                    for pin in request.pinned:
                        if pin.prefix not in found and line.startswith(pin.prefix):
                            found[pin.prefix] = line.rstrip("\n")
        missing = tuple(p.prefix for p in request.pinned if p.prefix not in found)
        text = "".join(f"{found[key]}\n" for key in sorted(found))
        target = request.target_path
        if text or target.exists():
            self._write_if_changed(target, text)
        return ModelPinnedRulingsResult(
            status=EnumPinnedRulingsStatus.MISSING
            if missing
            else EnumPinnedRulingsStatus.MIRRORED,
            rows_written=len(found),
            target_path=str(target),
            missing=missing,
            detail=(
                f"no archive roll in {request.rolls_directory} holds: {'; '.join(missing)}"
                if missing
                else ""
            ),
        )

    @staticmethod
    def _write_if_changed(target: Path, text: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_text(encoding="utf-8") == text:
            return
        held = target.with_suffix(target.suffix + ".tmp")
        held.write_text(text, encoding="utf-8")
        held.replace(target)
