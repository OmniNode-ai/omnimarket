# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor's ledger window is read one split archive at a time through the source read cache (OMN-19998).

Golden: the rows equal those of the combined ``--archives`` read of the same files, and a second cycle with unchanged sources reads
only the live ledger (the counting reader is the failing control: it makes zero archive reads). A changed archive is a miss and is read
again, an archive older than the window is never read. Error chain: a reader that refuses propagates (the step refuses as it did), a
cache that cannot be reached still returns the full rows, a corrupt cached payload is read again. The named injected break (a key that
leaves the revision out) serves an archive's old rows after it was rewritten.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_factory_floor_effect.handlers.ledger_window_by_archive import (
    decode_rows,
    encode_rows,
    read_ledger_rows_by_archive,
    read_ledger_window,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_runtime import (
    ModelLedgerArchiveWindow,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    SourceRefusedError,
    parse_ledger_rows,
    parse_ledger_text,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader import (
    CachedSourceReader,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.handler_source_read_cache import (
    HandlerSourceReadCache,
)
from omnimarket.nodes.node_source_read_cache_effect.models.model_source_read_cache import (
    ModelSourceCacheKey,
)
from tests.nodes.node_source_read_cache_effect.support import (
    PREFIX,
    TTL_BACKSTOP,
    DownBackend,
    memory_reader,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
KINDS = frozenset({"CLAIM", "TERMINAL", "STATUS"})
ARCHIVE = ModelLedgerArchiveWindow(
    ledger_path_env="TEST_LEDGER_PATH",
    ledger_record_env="TEST_LEDGER_RECORD",
    archive_subdir="archive",
    archive_glob="LEDGER_*-split.md",
    archive_date_pattern=r"\d{4}-\d{2}-\d{2}",
)


def _row(moment: datetime, kind: str, lane: str, text: str = "x") -> str:
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{stamp} | {kind} | lane={lane} | ticket=OMN-1 | {text}"


def _write(path: Path, rows: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _tree(tmp_path: Path) -> Path:
    """A live ledger and archives: one before the window, three inside it (one with a row before the window's exact start)."""
    day = timedelta(days=1)
    live = tmp_path / "LEDGER.md"
    _write(live, [_row(NOW - timedelta(hours=1), "CLAIM", "lane-live")])
    _write(
        tmp_path / "archive" / "LEDGER_2026-10-20-split.md",
        [_row(NOW - 22 * day, "CLAIM", "lane-old")],
    )
    _write(
        tmp_path / "archive" / "LEDGER_2026-10-30-split.md",
        [
            _row(NOW - timedelta(days=14, hours=3), "CLAIM", "lane-edge"),
            _row(NOW - timedelta(days=13), "TERMINAL", "lane-a"),
            _row(NOW - timedelta(days=13), "OPERATOR-CONSENT", "lane-a"),
        ],
    )
    _write(
        tmp_path / "archive" / "LEDGER_2026-11-05-split.md",
        [
            _row(NOW - 7 * day, "CLAIM", "lane-b"),
            _row(NOW - 6 * day, "OPERATOR-CONSENT", "lane-b"),
        ],
    )
    _write(
        tmp_path / "archive" / "LEDGER_2026-11-11-split.md",
        [_row(NOW - 1 * day, "STATUS", "lane-c"), _row(NOW - 1 * day, "MSG", "lane-c")],
    )
    return live


class Reader:
    """Stands in for the plugin's ledger reader: prints the rows of one file at or after ``--since``, counting each file read."""

    def __init__(self) -> None:
        self.files: list[str] = []

    def __call__(self, args: Sequence[str], timeout_seconds: int) -> str:
        path = Path(args[args.index("--ledger") + 1])
        since = args[args.index("--since") + 1]
        self.files.append(path.name)
        lines = [
            (n, line)
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if line.split(" | ")[0] >= since
        ]
        return f"ROWS n={len(lines)}\n" + "".join(f"L{n} {line}\n" for n, line in lines)


def _combined(live: Path, now: datetime, window_days: int = 14) -> str:
    """The combined ``--archives`` read: every in-window row of every file, archive rows prefixed with their file name."""
    since = (now - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = ["ROWS n=0"]
    for path in [*sorted((live.parent / "archive").glob("LEDGER_*-split.md")), live]:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.split(" | ")[0] >= since:
                prefix = f"{path.name}:" if path != live else ""
                out.append(f"{prefix}L{n} {line}")
    return "\n".join(out) + "\n"


def _by_archive(
    live: Path, reader: Reader, cache: CachedSourceReader, now: datetime = NOW
):  # type: ignore[no-untyped-def]
    return read_ledger_rows_by_archive(
        live_path=live,
        archive=ARCHIVE,
        window_days=14,
        cut_chars=1200,
        timeout_seconds=30,
        now=now,
        kinds=KINDS,
        cache=cache,
        run=reader,
    )


def test_golden_the_rows_equal_the_combined_archives_read(tmp_path: Path) -> None:
    live = _tree(tmp_path)
    cache, _ = memory_reader()
    got = _by_archive(live, Reader(), cache)
    assert got == parse_ledger_text(_combined(live, NOW), KINDS)
    # the row before the window's exact start is dropped, the non-core kind keeps only its lane's newest row
    assert "lane-edge" not in {r.lane for r in got}
    assert [r.kind for r in got if r.lane == "lane-a"] == [
        "TERMINAL",
        "OPERATOR-CONSENT",
    ]


def test_a_second_cycle_with_unchanged_sources_reads_only_the_live_ledger(
    tmp_path: Path,
) -> None:
    live = _tree(tmp_path)
    cache, _ = memory_reader()
    first_reader = Reader()
    first = _by_archive(live, first_reader, cache)
    assert sorted(first_reader.files) == [
        "LEDGER.md",
        "LEDGER_2026-10-30-split.md",
        "LEDGER_2026-11-05-split.md",
        "LEDGER_2026-11-11-split.md",
    ]
    second_reader = Reader()
    second = _by_archive(live, second_reader, cache, NOW + timedelta(minutes=5))
    assert second_reader.files == ["LEDGER.md"], (
        "an archive was read from its file again"
    )
    assert second == first
    assert cache.tally.hits == 3


def test_a_changed_archive_is_read_again_and_a_new_live_row_needs_no_archive_read(
    tmp_path: Path,
) -> None:
    live = _tree(tmp_path)
    cache, _ = memory_reader()
    _by_archive(live, Reader(), cache)
    archive = tmp_path / "archive" / "LEDGER_2026-11-05-split.md"
    archive.write_text(
        archive.read_text(encoding="utf-8")
        + _row(NOW - timedelta(days=5), "CLAIM", "lane-new")
        + "\n",
        encoding="utf-8",
    )
    reader = Reader()
    rows = _by_archive(live, reader, cache)
    assert reader.files == ["LEDGER_2026-11-05-split.md", "LEDGER.md"]
    assert "lane-new" in {r.lane for r in rows}
    live.write_text(
        live.read_text(encoding="utf-8")
        + _row(NOW - timedelta(minutes=1), "CLAIM", "lane-live2")
        + "\n",
        encoding="utf-8",
    )
    reader = Reader()
    rows = _by_archive(live, reader, cache)
    assert reader.files == ["LEDGER.md"]
    assert "lane-live2" in {r.lane for r in rows}


def test_an_archive_older_than_the_window_is_never_read(tmp_path: Path) -> None:
    live = _tree(tmp_path)
    cache, _ = memory_reader()
    reader = Reader()
    _by_archive(live, reader, cache)
    assert "LEDGER_2026-10-20-split.md" not in reader.files


def test_error_chain_a_refusing_reader_propagates_and_stores_nothing(
    tmp_path: Path,
) -> None:
    live = _tree(tmp_path)
    cache, backend = memory_reader()

    def refusing(args: Sequence[str], timeout_seconds: int) -> str:
        raise SourceRefusedError("the reader refused")

    with pytest.raises(SourceRefusedError, match="the reader refused"):
        read_ledger_rows_by_archive(
            live_path=live,
            archive=ARCHIVE,
            window_days=14,
            cut_chars=1200,
            timeout_seconds=30,
            now=NOW,
            kinds=KINDS,
            cache=cache,
            run=refusing,
        )
    assert backend.keys == []


def test_error_chain_an_unreachable_cache_still_returns_the_full_rows(
    tmp_path: Path,
) -> None:
    live = _tree(tmp_path)
    cache = CachedSourceReader(
        HandlerSourceReadCache(DownBackend(), PREFIX, TTL_BACKSTOP)
    )
    got = _by_archive(live, Reader(), cache)
    assert got == parse_ledger_text(_combined(live, NOW), KINDS)
    assert cache.tally.unavailable == 3


def test_error_chain_a_corrupt_cached_payload_is_read_again(tmp_path: Path) -> None:
    live = _tree(tmp_path)
    cache, backend = memory_reader()
    _by_archive(live, Reader(), cache)
    for key in backend.keys:
        backend.put(key, "not base64 deflate", TTL_BACKSTOP)
    reader = Reader()
    got = _by_archive(live, reader, cache)
    assert len(reader.files) == 4
    assert got == parse_ledger_text(_combined(live, NOW), KINDS)


def test_the_rows_round_trip_and_a_bad_payload_is_refused() -> None:
    rows = parse_ledger_rows(_row(NOW, "CLAIM", "lane-a") + "\n")
    # a row line needs the reader's L<n> prefix
    assert rows == ()
    rows = parse_ledger_rows("L1 " + _row(NOW, "CLAIM", "lane-a") + "\n")
    assert len(rows) == 1
    assert decode_rows(encode_rows(rows)) == rows
    with pytest.raises(ValueError, match="cannot be decoded"):
        decode_rows("###")


def test_the_split_of_the_text_parser_returns_what_it_returned(tmp_path: Path) -> None:
    text = "".join(
        f"L{n} {line}\n"
        for n, line in enumerate(
            [
                _row(NOW - timedelta(hours=3), "CLAIM", "lane-a"),
                _row(NOW - timedelta(hours=2), "OPERATOR-CONSENT", "lane-a", "first"),
                _row(NOW - timedelta(hours=1), "OPERATOR-CONSENT", "lane-a", "second"),
            ],
            1,
        )
    )
    got = parse_ledger_text(text, KINDS)
    assert [(r.kind, r.text) for r in got] == [
        ("CLAIM", "x"),
        ("OPERATOR-CONSENT", "second"),
    ]


def test_without_a_cache_or_with_another_record_the_window_read_is_the_old_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = _tree(tmp_path)
    seen: list[str] = []

    def old(**kwargs: object) -> tuple[()]:
        seen.append("old")
        return ()

    from omnimarket.nodes.node_factory_floor_effect.handlers import (
        ledger_window_by_archive as module,
    )

    monkeypatch.setattr(module, "read_ledger_rows", old)
    cache, _ = memory_reader()
    common = {
        "window_days": 14,
        "cut_chars": 1200,
        "timeout_seconds": 30,
        "plugin_root": tmp_path,
        "plugin_root_env": "CLAUDE_PLUGIN_ROOT",
        "ledger_rows_file": None,
        "kinds": KINDS,
        "archive": ARCHIVE,
        "now": NOW,
    }
    env = {"TEST_LEDGER_PATH": str(live), "TEST_LEDGER_RECORD": "file"}
    read_ledger_window(source=None, archive_cache=None, environ=env, **common)  # type: ignore[arg-type]
    read_ledger_window(
        source="db",
        archive_cache=cache,
        environ=env,
        **common,  # type: ignore[arg-type]
    )
    read_ledger_window(
        source=None,
        archive_cache=cache,
        environ={"TEST_LEDGER_PATH": str(live)},
        **common,  # type: ignore[arg-type]
    )
    assert seen == ["old", "old", "old"]
    read_ledger_window(
        source="file",
        archive_cache=cache,
        environ=env,
        archive_runner=Reader(),
        **common,  # type: ignore[arg-type]
    )
    assert seen == ["old", "old", "old"]


def test_named_injected_break_a_key_without_the_revision_serves_an_archives_old_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = _tree(tmp_path)
    cache, _ = memory_reader()
    _by_archive(live, Reader(), cache)
    archive = tmp_path / "archive" / "LEDGER_2026-11-05-split.md"
    archive.write_text(
        archive.read_text(encoding="utf-8")
        + _row(NOW - timedelta(days=5), "CLAIM", "lane-new")
        + "\n",
        encoding="utf-8",
    )
    assert "lane-new" in {r.lane for r in _by_archive(live, Reader(), cache)}
    monkeypatch.setattr(
        ModelSourceCacheKey,
        "render",
        lambda self, prefix: f"{prefix}{self.source_id}:{self.params_digest}",
    )
    broken_cache, _ = memory_reader()
    stale = tmp_path / "archive" / "LEDGER_2026-11-05-split.md"
    before = stale.read_text(encoding="utf-8")
    _by_archive(live, Reader(), broken_cache)
    stale.write_text(
        before + _row(NOW - timedelta(days=4), "CLAIM", "lane-newer") + "\n",
        encoding="utf-8",
    )
    assert "lane-newer" not in {
        r.lane for r in _by_archive(live, Reader(), broken_cache)
    }


def test_an_overlay_without_the_archive_block_reads_the_window_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_factory_floor_effect.handlers import (
        ledger_window_by_archive as module,
    )

    live = _tree(tmp_path)
    seen: list[str] = []

    def old(**kwargs: object) -> tuple[()]:
        seen.append("old")
        return ()

    monkeypatch.setattr(module, "read_ledger_rows", old)
    cache, _ = memory_reader()
    read_ledger_window(
        window_days=14,
        cut_chars=1200,
        timeout_seconds=30,
        source="file",
        plugin_root=tmp_path,
        plugin_root_env="CLAUDE_PLUGIN_ROOT",
        ledger_rows_file=None,
        kinds=KINDS,
        archive_cache=cache,
        archive=None,
        environ={"TEST_LEDGER_PATH": str(live), "TEST_LEDGER_RECORD": "file"},
        now=NOW,
    )
    assert seen == ["old"]
    assert cache.tally.hits == 0
    assert cache.tally.misses == 0


def test_the_factory_floor_overlay_accepts_the_archive_block_and_works_without_it() -> (
    None
):
    from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_runtime import (
        ModelFactoryFloorOverlay,
    )

    fields = ModelFactoryFloorOverlay.model_fields
    assert fields["ledger_archives"].default is None
    assert fields["ledger_archives"].annotation == ModelLedgerArchiveWindow | None
