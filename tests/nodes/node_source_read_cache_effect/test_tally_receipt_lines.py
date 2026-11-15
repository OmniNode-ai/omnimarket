# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The receipts a cycle wrote become the publisher's ``source-cache:`` log lines (OMN-19998).

Golden: a receipt the real ``write_tally_receipt`` wrote for a reader whose tally counted two hits and one
miss comes back as the one line naming them. Error chain: a receipt that is not JSON becomes a line naming
the exception's class only, and a cache that is off names its reason after ``off=``; a missing receipt
directory is no lines at all. Failing control: a receipt written before ``since`` stays unlisted, so
listing both receipts is the failure the since filter exists to prevent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader import (
    CachedSourceReader,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader_factory import (
    write_tally_receipt,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.tally_receipt_lines import (
    read_tally_lines,
)

pytestmark = pytest.mark.unit

CYCLE_START = datetime(2026, 11, 13, 11, 0, 0, tzinfo=UTC)
EARLIER = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC)
VALKEY_REASON = "valkey: host or port variable not set (A, B)"


def test_golden_a_receipt_becomes_one_line_naming_the_cache(tmp_path: Path) -> None:
    reader = CachedSourceReader(None)
    reader.tally.hits = 2
    reader.tally.misses = 1
    write_tally_receipt(reader, tmp_path, "factory_floor", now=LATER)
    assert read_tally_lines(tmp_path, CYCLE_START) == [
        "source-cache: step=factory_floor backend=none hits=2 misses=1 stores=0"
        " bypassed=0 unavailable=0 source_reads=0 source_seconds=0.0 off=none"
    ]


def test_a_receipt_written_before_since_is_skipped(tmp_path: Path) -> None:
    reader = CachedSourceReader(None)
    reader.tally.hits = 1
    write_tally_receipt(reader, tmp_path, "factory_floor", now=EARLIER)
    assert read_tally_lines(tmp_path, CYCLE_START) == []


def test_error_chain_a_receipt_that_is_not_json_is_unreadable(tmp_path: Path) -> None:
    reader = CachedSourceReader(None)
    receipt = write_tally_receipt(reader, tmp_path, "factory_floor", now=LATER)
    receipt.write_text("not json", encoding="utf-8")
    assert read_tally_lines(tmp_path, CYCLE_START) == [
        "source-cache: step=factory_floor receipt unreadable (JSONDecodeError)"
    ]


def test_a_cache_that_is_off_says_why_after_off(tmp_path: Path) -> None:
    reader = CachedSourceReader(None, disabled_reason=VALKEY_REASON)
    write_tally_receipt(reader, tmp_path, "factory_floor", now=LATER)
    lines = read_tally_lines(tmp_path, CYCLE_START)
    assert len(lines) == 1
    assert lines[0].endswith(f"off={VALKEY_REASON}")


def test_a_missing_receipt_directory_is_no_lines(tmp_path: Path) -> None:
    assert read_tally_lines(tmp_path, CYCLE_START) == []


def test_failing_control_only_receipts_at_or_after_since(tmp_path: Path) -> None:
    earlier = CachedSourceReader(None)
    earlier.tally.hits = 1
    write_tally_receipt(earlier, tmp_path, "earlier_step", now=EARLIER)
    later = CachedSourceReader(None)
    later.tally.misses = 1
    write_tally_receipt(later, tmp_path, "factory_floor", now=LATER)
    assert read_tally_lines(tmp_path, CYCLE_START) == [
        "source-cache: step=factory_floor backend=none hits=0 misses=1 stores=0"
        " bypassed=0 unavailable=0 source_reads=0 source_seconds=0.0 off=none"
    ]
