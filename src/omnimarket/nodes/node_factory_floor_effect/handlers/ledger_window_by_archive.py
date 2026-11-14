# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor's ledger window, read one split archive at a time through the source read cache (OMN-19998).

Measured on a lab host with the real ledger: the window read took 16.6 s of a 27 s record step, 12.7 s of it the reader scanning every split
archive and 3.9 s parsing. An archive never changes once rolled, so its parsed rows are kept under its size and modification time and read
again only when either changes; the live ledger, the only file that moves between cycles, is read every cycle. The rows returned equal the
combined ``--archives`` read of the same files: the archives are read from the first day of the window, the rows before the window's exact
start are dropped, and the per-lane reduction runs once over the union.
"""

from __future__ import annotations

import base64
import re
import subprocess
import sys
import zlib
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import TypeAdapter

from omnimarket.models.model_factory_floor_input import ModelFloorLedgerRow
from omnimarket.models.model_source_read_cache import (
    ModelSourceCacheKey,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_runtime import (
    ModelLedgerArchiveWindow,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    SourceRefusedError,
    parse_ledger_rows,
    read_ledger_rows,
    reduce_ledger_rows,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader import (
    CachedSourceReader,
)

# The reader's arguments without the interpreter and script, and the seconds it may take, to its standard output.
ArchiveReaderRunner = Callable[[Sequence[str], int], str]

STAMP = "%Y-%m-%dT%H:%M:%SZ"
ROWS_FORMAT = 1
_ROWS = TypeAdapter(tuple[ModelFloorLedgerRow, ...])


def subprocess_archive_runner(reader: Path) -> ArchiveReaderRunner:
    """The runner that calls the plugin's ledger reader, refusing as the window read does when it times out or fails."""

    def run(args: Sequence[str], timeout_seconds: int) -> str:
        try:
            done = subprocess.run(
                [sys.executable, str(reader), *args],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SourceRefusedError(
                f"ledger reader {reader} did not finish within {timeout_seconds} s: repair the reader or supply ledger_rows_file"
            ) from exc
        if done.returncode:
            raise SourceRefusedError(
                f"ledger reader {reader} exited {done.returncode}: {done.stderr[:200]}; repair the reader or supply ledger_rows_file"
            )
        return done.stdout

    return run


def encode_rows(rows: Sequence[ModelFloorLedgerRow]) -> str:
    """The rows as one text value: their JSON, deflated, in base64."""
    packed = zlib.compress(_ROWS.dump_json(tuple(rows)), 1)
    return base64.b64encode(packed).decode("ascii")


def decode_rows(text: str) -> tuple[ModelFloorLedgerRow, ...]:
    """The rows ``encode_rows`` wrote; a payload that is not one raises ``ValueError``."""
    try:
        return _ROWS.validate_json(
            zlib.decompress(base64.b64decode(text, validate=True))
        )
    except (zlib.error, ValueError) as exc:
        raise ValueError("the cached ledger rows cannot be decoded") from exc


def _query(path: Path, since: str, cut_chars: int) -> list[str]:
    return [
        "query",
        "--source",
        "file",
        "--ledger",
        str(path),
        "--since",
        since,
        "--limit",
        "1000000",
        "--cut",
        str(cut_chars),
    ]


def read_ledger_rows_by_archive(
    *,
    live_path: Path,
    archive: ModelLedgerArchiveWindow,
    window_days: int,
    cut_chars: int,
    timeout_seconds: int,
    now: datetime,
    kinds: frozenset[str],
    cache: CachedSourceReader,
    run: ArchiveReaderRunner,
) -> tuple[ModelFloorLedgerRow, ...]:
    """The window's rows: each in-window archive through the cache, the live ledger every time, reduced once."""
    since_exact = now.astimezone(UTC) - timedelta(days=window_days)
    since_day = since_exact.replace(hour=0, minute=0, second=0, microsecond=0)
    since_day_stamp = since_day.strftime(STAMP)
    date_pattern = re.compile(archive.archive_date_pattern)
    merged: list[ModelFloorLedgerRow] = []
    for path in sorted(
        (live_path.parent / archive.archive_subdir).glob(archive.archive_glob)
    ):
        named = date_pattern.search(path.name)
        if named is not None and named.group(0) < since_day.strftime("%Y-%m-%d"):
            continue
        info = path.stat()

        def read_archive(path: Path = path) -> tuple[ModelFloorLedgerRow, ...]:
            return parse_ledger_rows(
                run(_query(path, since_day_stamp, cut_chars), timeout_seconds)
            )

        rows, _outcome = cache.read(
            ModelSourceCacheKey(
                source_id=f"ledger-archive:{path.name}",
                params_digest=ModelSourceCacheKey.digest(
                    {
                        "since_day": since_day_stamp,
                        "cut_chars": cut_chars,
                        "rows_format": ROWS_FORMAT,
                    }
                ),
                revision=f"{info.st_size}-{info.st_mtime_ns}",
            ),
            read_archive,
            encode_rows,
            decode_rows,
        )
        merged.extend(row for row in rows if row.ts >= since_exact)
    merged.extend(
        parse_ledger_rows(
            run(
                _query(live_path, since_exact.strftime(STAMP), cut_chars),
                timeout_seconds,
            )
        )
    )
    return reduce_ledger_rows(merged, kinds)


def read_ledger_window(
    *,
    window_days: int,
    cut_chars: int,
    timeout_seconds: int,
    source: str | None,
    plugin_root: Path | None,
    plugin_root_env: str,
    ledger_rows_file: Path | None,
    kinds: frozenset[str],
    archive_cache: CachedSourceReader | None,
    archive: ModelLedgerArchiveWindow | None,
    environ: Mapping[str, str],
    now: datetime,
    archive_runner: ArchiveReaderRunner | None = None,
) -> tuple[ModelFloorLedgerRow, ...]:
    """The window read: by archive through the cache when the ledger record is the file and a cache is given, else as ``read_ledger_rows``."""
    live = environ.get(archive.ledger_path_env, "").strip() if archive else ""
    file_record = source == "file" or (
        source is None
        and archive is not None
        and environ.get(archive.ledger_record_env) == "file"
    )
    if (
        archive is not None
        and archive_cache is not None
        and ledger_rows_file is None
        and plugin_root is not None
        and file_record
        and live
        and Path(live).is_file()
    ):
        return read_ledger_rows_by_archive(
            live_path=Path(live),
            archive=archive,
            window_days=window_days,
            cut_chars=cut_chars,
            timeout_seconds=timeout_seconds,
            now=now,
            kinds=kinds,
            cache=archive_cache,
            run=archive_runner
            or subprocess_archive_runner(plugin_root / "scripts" / "onex_ledger.py"),
        )
    return read_ledger_rows(
        window_days=window_days,
        cut_chars=cut_chars,
        timeout_seconds=timeout_seconds,
        source=source,
        plugin_root=plugin_root,
        plugin_root_env=plugin_root_env,
        ledger_rows_file=ledger_rows_file,
        kinds=kinds,
    )
