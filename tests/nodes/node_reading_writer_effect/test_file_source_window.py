# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A windowed ledger file read reads the window, not the whole record (OMN-23081).

The floor reads fourteen days of a ledger whose rolls hold months, inside a step with a hard bound. The file source
passes over a roll cut more than a day before the earliest window without opening it, and passes over a line stamped
before the window without parsing it. The answers are the rows a read of every line gives.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_reading_writer_effect.adapters import ledger_adapters
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    FileLedgerRowSource,
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelLedgerQuery

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 9, 0, 0, tzinfo=UTC)
WINDOW_DAYS = 14


def row(stamp: str, lane: str = "lane-a") -> str:
    return f"{stamp} | STATUS | lane={lane} | ticket=OMN-1 | a row"


def query(**fields: Any) -> ModelLedgerQuery:
    base: dict[str, Any] = {
        "since": NOW - timedelta(days=WINDOW_DAYS),
        "until": NOW + timedelta(days=1),
        "row_type": "STATUS",
    }
    return ModelLedgerQuery(**{**base, **fields})


def write(path: Path, *lines: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def lay_out(root: Path) -> Path:
    """A live file and rolls cut on 10-10, 10-25 (the day before the window's day) and 10-26 (inside it)."""
    live = write(
        root / "LEDGER.md",
        row("2026-10-20T00:00:00Z", "live-before"),
        row("2026-11-08T09:00:00Z", "live-in"),
    )
    archive = root / "archive"
    write(
        archive / "LEDGER_2026-10-10-split.md", row("2026-10-10T08:00:00Z", "roll-old")
    )
    write(
        archive / "LEDGER_2026-10-25-split.md",
        row("2026-10-25T23:00:00Z", "roll-before"),
        row("2026-10-26T00:30:00Z", "roll-edge"),
    )
    write(
        archive / "LEDGER_2026-10-26-split.md", row("2026-10-26T03:00:00Z", "roll-in")
    )
    return live


def count_opens(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    opened: list[str] = []
    real_open = Path.open

    def counting(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.suffix == ".md":
            opened.append(self.name)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting)
    return opened


def lanes(source: FileLedgerRowSource) -> list[str]:
    read = source.read(query())
    assert read.error is None
    return sorted(r.lane or "" for r in read.rows)


def test_a_roll_named_more_than_a_day_before_the_window_is_not_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = lay_out(tmp_path)
    opened = count_opens(monkeypatch)
    assert lanes(FileLedgerRowSource(live, live.parent / "archive")) == [
        "live-in",
        "roll-edge",
        "roll-in",
    ]
    assert "LEDGER_2026-10-10-split.md" not in opened
    assert "LEDGER_2026-10-25-split.md" in opened
    assert "LEDGER_2026-10-26-split.md" in opened


def test_a_roll_named_the_day_before_the_window_is_kept(tmp_path: Path) -> None:
    """The name is the cutting host's local day: its last rows can be stamped a day later in UTC."""
    live = lay_out(tmp_path)
    # The window starts 2026-10-26T00:00Z; the roll named 10-25 holds a row stamped 10-26T13:00Z.
    write(
        live.parent / "archive" / "LEDGER_2026-10-25-split.md",
        row("2026-10-26T13:00:00Z", "late-in-roll"),
    )
    read = FileLedgerRowSource(live, live.parent / "archive").read(query())
    assert "late-in-roll" in {r.lane for r in read.rows}


def test_a_roll_with_no_day_in_its_name_is_never_passed_over(tmp_path: Path) -> None:
    live = lay_out(tmp_path)
    write(
        live.parent / "archive" / "LEDGER_restored.md",
        row("2026-11-01T00:00:00Z", "restored"),
    )
    assert "restored" in lanes(FileLedgerRowSource(live, live.parent / "archive"))


def test_a_line_before_the_window_is_not_parsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = lay_out(tmp_path)
    parsed: list[str] = []
    real = ledger_adapters.grammar.parse_row

    def counting(line: str, **kwargs: Any) -> Any:
        parsed.append(line)
        return real(line, **kwargs)

    monkeypatch.setattr(ledger_adapters.grammar, "parse_row", counting)
    lanes(FileLedgerRowSource(live, live.parent / "archive"))
    assert not any("live-before" in line for line in parsed)
    assert any("live-in" in line for line in parsed)


def test_the_windowed_read_gives_the_rows_of_a_read_of_every_line(
    tmp_path: Path,
) -> None:
    live = lay_out(tmp_path)
    archive = live.parent / "archive"
    every_line = [
        line
        for path in [live, *sorted(archive.iterdir())]
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    for since in (
        NOW - timedelta(days=WINDOW_DAYS),
        NOW - timedelta(days=1),
        NOW - timedelta(days=40),
    ):
        asked = query(since=since)
        windowed = FileLedgerRowSource(live, archive).read(asked)
        whole = InMemoryLedger(every_line).read(asked)
        assert sorted(r.raw for r in windowed.rows) == sorted(r.raw for r in whole.rows)


def test_a_windowed_read_that_drops_a_row_of_the_window_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failing control, a named injected break: a source that passes over every dated roll loses the window's rows."""
    live = lay_out(tmp_path)
    monkeypatch.setattr(ledger_adapters, "_roll_ends_before", lambda *_: True)
    assert "roll-in" not in lanes(FileLedgerRowSource(live, live.parent / "archive"))
