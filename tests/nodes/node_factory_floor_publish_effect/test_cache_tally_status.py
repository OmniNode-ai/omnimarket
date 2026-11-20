# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26241: the publish status carries each board step's cache tally, and a step that stores nothing and reads its source for the
contract's number of cycles in a row gets a red row.

Golden (``cache_tally``): three cycles in which beta_board stored nothing and read its source give a red row for it in the served status
and in the run result; two such cycles give none. Error chains: a cycle that stored resets the count; folding the same cycle twice counts
it once; an unreadable count file starts from zero and the publish still runs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    HandlerFactoryFloorPublish,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishRequest,
    ModelFactoryFloorPublishResult,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader import (
    CachedSourceReader,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader_factory import (
    write_tally_receipt,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.tally_history import (
    fold_cache_tally,
)
from tests.nodes.node_factory_floor_publish_effect.test_cycle_timing import STATUS
from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    PLUGIN,
    LocalSsh,
    _home,
    _overlay,
)

pytestmark = pytest.mark.unit

START = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)
CYCLE = timedelta(minutes=5)
STREAK_FILE = ".onex_state/source-cache-streaks/streaks.json"


def _reader(
    stores: int, source_reads: int, source_seconds: float
) -> CachedSourceReader:
    reader = CachedSourceReader(None, backend_name="valkey")
    reader.tally.stores = stores
    reader.tally.source_reads = source_reads
    reader.tally.source_seconds = source_seconds
    return reader


def _cycle(
    tmp_path: Path,
    home: Path,
    index: int,
    beta: CachedSourceReader,
) -> tuple[ModelFactoryFloorPublishResult, dict[str, object]]:
    """One publish at ``START`` plus ``index`` cycles, after the steps wrote their receipts a second into the cycle."""
    now = START + CYCLE * index
    write_tally_receipt(beta, home, "beta_board", now=now + timedelta(seconds=1))
    write_tally_receipt(
        _reader(2, 0, 0.0), home, "factory_floor", now=now + timedelta(seconds=1)
    )
    request = ModelFactoryFloorPublishRequest(
        home=str(home), overlay=_overlay(tmp_path), now=now
    )
    result = HandlerFactoryFloorPublish(LocalSsh()).handle(request, environ=PLUGIN)
    status = json.loads((tmp_path / "site" / STATUS).read_text(encoding="utf-8"))
    return result, status["cache_tally"]


def test_cache_tally_three_cycles_of_no_store_and_source_reads_raise_a_red_row(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    for index in range(2):
        result, tally = _cycle(tmp_path, home, index, _reader(0, 2, 50.0))
        assert tally["red_rows"] == []
        assert result.cache_red_rows == ()
    result, tally = _cycle(tmp_path, home, 2, _reader(0, 2, 50.0))
    beta = next(s for s in tally["steps"] if s["step"] == "beta_board")
    assert beta["stores"] == 0
    assert beta["source_reads"] == 2
    assert beta["source_seconds"] == 50.0
    assert beta["no_store_cycles"] == 3
    assert [row["step"] for row in tally["red_rows"]] == ["beta_board"]
    assert len(result.cache_red_rows) == 1
    assert (
        "beta_board stored nothing and read its source in 3 cycles in a row"
        in (result.cache_red_rows[0])
    )
    # The step that stored carries its tally and no red row.
    assert (
        next(s for s in tally["steps"] if s["step"] == "factory_floor")["stores"] == 2
    )


def test_cache_tally_a_cycle_that_stored_resets_the_count(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _cycle(tmp_path, home, 0, _reader(0, 2, 50.0))
    _cycle(tmp_path, home, 1, _reader(0, 2, 50.0))
    _cycle(tmp_path, home, 2, _reader(1, 2, 50.0))
    _, tally = _cycle(tmp_path, home, 3, _reader(0, 2, 50.0))
    assert tally["red_rows"] == []
    assert (
        next(s for s in tally["steps"] if s["step"] == "beta_board")["no_store_cycles"]
        == 1
    )


def test_cache_tally_a_cycle_that_read_nothing_is_not_a_no_store_cycle(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    for index in range(4):
        _, tally = _cycle(tmp_path, home, index, _reader(0, 0, 0.0))
    assert tally["red_rows"] == []


def test_cache_tally_folding_the_same_cycle_twice_counts_it_once(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    write_tally_receipt(
        _reader(0, 2, 50.0), home, "beta_board", now=START + timedelta(seconds=1)
    )
    first = fold_cache_tally(home, START)
    second = fold_cache_tally(home, START)
    assert first.steps[0].no_store_cycles == 1
    assert second.steps[0].no_store_cycles == 1


def test_cache_tally_an_unreadable_count_file_starts_from_zero(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / STREAK_FILE).parent.mkdir(parents=True)
    (home / STREAK_FILE).write_text("not json", encoding="utf-8")
    write_tally_receipt(
        _reader(0, 2, 50.0), home, "beta_board", now=START + timedelta(seconds=1)
    )
    folded = fold_cache_tally(home, START)
    assert folded.steps[0].no_store_cycles == 1
    assert folded.red_rows == ()


def test_cache_tally_a_receipt_of_an_earlier_cycle_is_not_this_cycles(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    write_tally_receipt(
        _reader(0, 2, 50.0), home, "beta_board", now=START - timedelta(hours=1)
    )
    assert fold_cache_tally(home, START).steps == ()
