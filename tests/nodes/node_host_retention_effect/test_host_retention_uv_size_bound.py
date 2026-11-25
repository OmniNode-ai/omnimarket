# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``uv cache size`` walks every file of a cache, so the retention pass never runs it without a bound.

The contract owns the ceiling (``retention_bounds.uv_cache_max_timeout_seconds``). A uv cache row whose ``timeout_seconds`` is above
it, or is not a finite number, is refused before the pass runs uv or reads anything. A real ``uv`` stand-in that outlives the declared
timeout is killed and read as an error, and one that answers in time is read as a size.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_host_retention_effect.handlers.handler_host_retention import (
    HandlerHostRetention,
    load_bounds,
)
from omnimarket.nodes.node_host_retention_effect.handlers.host_retention_ports import (
    ProcessUvCache,
)
from omnimarket.nodes.node_host_retention_effect.models.model_host_retention import (
    ModelHostRetentionRequest,
    ModelUvCache,
)
from omnimarket.nodes.node_temp_retention_effect.handlers.handler_temp_retention import (
    ProcessReferences,
)
from tests.nodes.node_host_retention_effect.host_retention_fakes import (
    NOW,
    FakeUv,
    FakeUvArchive,
    make_ports,
    overlay_file,
)

pytestmark = pytest.mark.unit

_GIB = 2**30


class CountingUv(FakeUv):
    """A uv port that records every size read and the timeout it was given."""

    def __init__(self, size: int = 0, after: int = 0) -> None:
        super().__init__(size=size, after=after)
        self.size_timeouts: list[float] = []

    def size_bytes(self, uv_binary: str, cache_dir: str, timeout_s: float) -> int:
        self.size_timeouts.append(timeout_s)
        return super().size_bytes(uv_binary, cache_dir, timeout_s)


def _row(path: Path, timeout_seconds: float) -> dict[str, object]:
    return {
        "path": str(path),
        "uv_binary": "uv",
        "min_gib": 20.0,
        "ci_prune": False,
        "timeout_seconds": timeout_seconds,
    }


def _run(tmp_path: Path, row: dict[str, object], uv: CountingUv):
    handler = HandlerHostRetention(
        now=lambda: NOW,
        references=lambda: ProcessReferences(),
        ports=make_ports(uv=uv, uv_archive=FakeUvArchive()),
    )
    return handler.handle(
        ModelHostRetentionRequest(
            host_name="pool-a",
            overlay_file=overlay_file(tmp_path, uv_caches=[row]),
            dry_run=True,
        )
    )


def test_contract_declares_the_uv_size_ceiling() -> None:
    bound = load_bounds().uv_cache_max_timeout_seconds
    assert 0 < bound <= 3600


def test_row_above_the_ceiling_is_refused_and_uv_never_runs(tmp_path: Path) -> None:
    path = tmp_path / "uv-cache"
    path.mkdir()
    uv = CountingUv(size=50 * _GIB, after=40 * _GIB)
    too_long = load_bounds().uv_cache_max_timeout_seconds + 1

    result = _run(tmp_path, _row(path, too_long), uv)

    assert result.status == "refused"
    assert any("timeout_seconds" in text for text in result.refusals)
    assert uv.size_timeouts == []


def test_row_at_the_ceiling_measures_with_that_timeout(tmp_path: Path) -> None:
    """Positive control: the refusal above is the ceiling, not a broken fixture."""
    path = tmp_path / "uv-cache"
    path.mkdir()
    uv = CountingUv(size=50 * _GIB, after=40 * _GIB)
    ceiling = load_bounds().uv_cache_max_timeout_seconds

    result = _run(tmp_path, _row(path, ceiling), uv)

    assert result.status == "ok"
    assert uv.size_timeouts == [ceiling]


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_a_timeout_that_is_not_finite_is_not_a_declaration(value: float) -> None:
    with pytest.raises(ValidationError):
        ModelUvCache.model_validate(
            {
                "path": "/x/uv-cache",
                "uv_binary": "uv",
                "min_gib": 20.0,
                "timeout_seconds": value,
            }
        )


def _fake_uv(tmp_path: Path, body: str) -> str:
    script = tmp_path / "uv"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


def test_a_uv_that_outlives_the_timeout_is_killed_and_read_as_an_error(
    tmp_path: Path,
) -> None:
    uv = _fake_uv(tmp_path, "sleep 30")
    with pytest.raises(OSError, match="no answer"):
        ProcessUvCache().size_bytes(uv, str(tmp_path), 0.5)


def test_a_uv_that_answers_in_time_is_read_as_a_size(tmp_path: Path) -> None:
    """Known good for the control above: same stand-in shape, answers at once."""
    uv = _fake_uv(tmp_path, "echo 12345")
    assert ProcessUvCache().size_bytes(uv, str(tmp_path), 5.0) == 12345
