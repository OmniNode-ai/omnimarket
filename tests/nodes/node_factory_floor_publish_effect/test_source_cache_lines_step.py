# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: a publish run reports what the source read cache did, so a cycle's log shows a cache hit or a miss.

Each board step writes a tally receipt under the registry home, and the publisher captured the step's output and never printed it, so no
log of a cycle held a hit, a miss or the reason the cache was off. The run result now carries the ``source-cache:`` line of each receipt
written during the run.

Golden chain: a receipt written during the run shows in the result with its counts. Error chains: a receipt left by an earlier run is not
reported as this one's (the named injected break: dropping the start-of-run filter makes that test fail); a receipt that cannot be
parsed is reported as unreadable and the run still publishes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    HandlerFactoryFloorPublish,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishResult,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader import (
    CachedSourceReader,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader_factory import (
    write_tally_receipt,
)
from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    PLUGIN,
    Scripted,
    _refresh_request,
)

pytestmark = pytest.mark.unit

STEP = "factory_floor"


def _publish(tmp_path: Path) -> tuple[ModelFactoryFloorPublishResult, Path]:
    request = _refresh_request(tmp_path)
    runner = Scripted({"--json": (0, b"[]", b"")})
    result = HandlerFactoryFloorPublish(runner).handle(request, environ=PLUGIN)
    return result, Path(request.home or "")


def _tally(hits: int, misses: int) -> CachedSourceReader:
    reader = CachedSourceReader(None, backend_name="valkey")
    reader.tally.hits = hits
    reader.tally.misses = misses
    return reader


def test_golden_a_receipt_written_during_the_run_is_in_the_result(
    tmp_path: Path,
) -> None:
    request = _refresh_request(tmp_path)
    write_tally_receipt(
        _tally(2, 1),
        Path(request.home or ""),
        STEP,
        now=datetime.now(UTC) + timedelta(hours=1),
    )
    result = HandlerFactoryFloorPublish(Scripted({"--json": (0, b"[]", b"")})).handle(
        request, environ=PLUGIN
    )
    assert len(result.source_cache_lines) == 1
    assert result.source_cache_lines[0].startswith(
        f"source-cache: step={STEP} backend=valkey hits=2 misses=1 "
    )


def test_error_control_a_receipt_from_an_earlier_run_is_not_this_runs(
    tmp_path: Path,
) -> None:
    request = _refresh_request(tmp_path)
    write_tally_receipt(
        _tally(5, 0),
        Path(request.home or ""),
        STEP,
        now=datetime.now(UTC) - timedelta(hours=1),
    )
    result = HandlerFactoryFloorPublish(Scripted({"--json": (0, b"[]", b"")})).handle(
        request, environ=PLUGIN
    )
    assert result.source_cache_lines == ()


def test_error_chain_an_unreadable_receipt_is_reported_and_the_run_publishes(
    tmp_path: Path,
) -> None:
    request = _refresh_request(tmp_path)
    receipt = write_tally_receipt(
        _tally(0, 0),
        Path(request.home or ""),
        STEP,
        now=datetime.now(UTC) + timedelta(hours=1),
    )
    receipt.write_text("not json", encoding="utf-8")
    result = HandlerFactoryFloorPublish(Scripted({"--json": (0, b"[]", b"")})).handle(
        request, environ=PLUGIN
    )
    assert result.source_cache_lines == (
        f"source-cache: step={STEP} receipt unreadable (JSONDecodeError)",
    )
    assert result.published


def test_no_receipt_no_lines(tmp_path: Path) -> None:
    result, _home = _publish(tmp_path)
    assert result.source_cache_lines == ()
