# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden, error and failing-control tests for the declared-clone tick (OMN-23557).

Real git layouts from ``tests/helpers/kbi_clone_coverage_layout.py``, an in-memory bus
wrapped as a zero-argument ``open_bus`` callable, and a ``tmp_path`` state directory.
The tick runs ``asyncio.run`` internally, so the bus is created and started inside
``open_bus``'s async context manager.
"""

import json
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import ValidationError

from omnimarket.models.overlay_keys import ModelLabHostsTable
from omnimarket.nodes.node_canonical_clone_refresh_effect.handlers.handler_declared_clone_refresh import (
    HandlerDeclaredCloneRefresh,
)
from omnimarket.nodes.node_canonical_clone_refresh_effect.models.model_declared_clone_tick import (
    ModelDeclaredCloneTickOverlay,
)
from omnimarket.nodes.node_canonical_clone_refresh_effect.runtime_declared_clone_tick import (
    EXIT_INDETERMINATE,
    EXIT_OK,
    EXIT_REFUSED,
    tick,
)
from tests.helpers.kbi_clone_coverage_layout import (
    HomesTransport,
    Layout,
    build_layout,
    git,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    return build_layout(tmp_path)


def _overlay() -> ModelDeclaredCloneTickOverlay:
    return ModelDeclaredCloneTickOverlay(
        backend="inmemory",
        bus_lane=None,
        host_name_env="TICK_HOST",
        wait_seconds=5.0,
    )


def _run_tick(
    layout: Layout,
    state_dir: Path,
    env: dict[str, str],
    table: ModelLabHostsTable | None = None,
) -> int:
    """Run the tick synchronously; the bus is created, started and closed inside ``open_bus``."""

    @asynccontextmanager
    async def open_bus() -> AsyncIterator[EventBusInmemory]:
        bus = EventBusInmemory(environment="local", group="declared-clone-tick-test")
        await bus.start()
        try:
            yield bus
        finally:
            await bus.close()

    handler = HandlerDeclaredCloneRefresh(
        table if table is not None else layout.table,
        layout.repo_dir,
        HomesTransport(layout.homes),
    )
    return tick(
        overlay=_overlay(),
        env=env,
        state_dir=state_dir,
        open_bus=open_bus,
        handler=handler,
    )


def _read_evidence(state_dir: Path) -> dict[str, Any]:
    return json.loads((state_dir / "last-run.json").read_text())


def test_golden_tick_refreshes_h101_and_writes_evidence(
    layout: Layout, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    code = _run_tick(layout, state_dir, {"TICK_HOST": "h101"})
    assert code == EXIT_OK
    assert layout.head("h101") == layout.origin_tip
    ev = _read_evidence(state_dir)
    assert ev["exit_code"] == 0
    assert ev["host"] == "h101"
    assert ev["refused"] is False
    assert len(ev["outcomes"]) == 1
    o = ev["outcomes"][0]
    assert o["state"] == "refreshed"
    assert o["path"] == "$HOME/Code/kbrepo"
    assert o["host"] == "h101"
    # the other hosts' heads stay at layout.start
    assert layout.head("h053") == layout.start["h053"]
    assert layout.head("h051") == layout.start["h051"]


def test_second_tick_is_up_to_date(layout: Layout, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _run_tick(layout, state_dir, {"TICK_HOST": "h101"})
    code = _run_tick(layout, state_dir, {"TICK_HOST": "h101"})
    assert code == EXIT_OK
    ev = _read_evidence(state_dir)
    assert ev["outcomes"][0]["state"] == "up_to_date"


def test_error_missing_path(layout: Layout, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    shutil.rmtree(layout.clone_path("h101"))
    code = _run_tick(layout, state_dir, {"TICK_HOST": "h101"})
    assert code == EXIT_REFUSED
    ev = _read_evidence(state_dir)
    assert ev["refused"] is True
    o = ev["outcomes"][0]
    assert o["refusal"] == "path_missing"
    assert "h101" in o["message"]
    assert "$HOME/Code/kbrepo" in o["message"]


def test_error_cannot_fast_forward(layout: Layout, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    clone = layout.clone_path("h101")
    (clone / "local.txt").write_text("local\n")
    git(clone, "add", "local.txt")
    git(clone, "commit", "--quiet", "-m", "local only")
    code = _run_tick(layout, state_dir, {"TICK_HOST": "h101"})
    assert code == EXIT_REFUSED
    ev = _read_evidence(state_dir)
    assert ev["refused"] is True
    assert ev["outcomes"][0]["refusal"] == "not_fast_forwarded"


def test_failing_control_host_table_row_missing_clones(
    layout: Layout, tmp_path: Path
) -> None:
    """The host-table row the lab was missing: h101's clones emptied -> not_declared."""
    rows = []
    for row in layout.table.hosts:
        if row.name == "h101":
            rows.append(row.model_copy(update={"clones": []}))
        else:
            rows.append(row)
    table = ModelLabHostsTable(hosts=tuple(rows))
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    code = _run_tick(layout, state_dir, {"TICK_HOST": "h101"}, table=table)
    assert code == EXIT_REFUSED
    ev = _read_evidence(state_dir)
    assert ev["refused"] is True
    assert ev["outcomes"][0]["refusal"] == "not_declared"


def test_indeterminate_missing_env_variable(
    layout: Layout, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    code = _run_tick(layout, state_dir, {})
    assert code == EXIT_INDETERMINATE
    ev = _read_evidence(state_dir)
    assert ev["exit_code"] == 3
    assert ev["refused"] is True
    captured = capsys.readouterr()
    assert "INDETERMINATE" in captured.err


def test_overlay_model_validation() -> None:
    with pytest.raises(ValidationError):
        ModelDeclaredCloneTickOverlay(
            backend="redpanda",
            bus_lane=None,
            host_name_env="TICK_HOST",
            wait_seconds=5.0,
        )
    with pytest.raises(ValidationError):
        ModelDeclaredCloneTickOverlay(
            backend="inmemory",
            bus_lane="lane",
            host_name_env="TICK_HOST",
            wait_seconds=5.0,
        )
    with pytest.raises(ValidationError):
        ModelDeclaredCloneTickOverlay(
            backend="inmemory",
            bus_lane=None,
            host_name_env="TICK_HOST",
            wait_seconds=0.0,
        )
