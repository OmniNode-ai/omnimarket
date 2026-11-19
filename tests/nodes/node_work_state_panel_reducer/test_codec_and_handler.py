# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The panel reducer's codec and handler: the row's encoding, no database, restart, resolution and file reads (OMN-19998).

Each check returns what is wrong; each control is a named injected break that must make the same check report a problem.
"""

from __future__ import annotations

import ast
import builtins
import inspect
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.work_state_panel import ModelWorkStatePanelRow
from omnimarket.nodes.node_work_state_panel_reducer import state_codec
from omnimarket.nodes.node_work_state_panel_reducer.handlers import (
    handler_work_state_panel_reducer as handler_module,
)
from omnimarket.nodes.node_work_state_panel_reducer.handlers import (
    service_work_state_panel_reducer as service_module,
)
from omnimarket.nodes.node_work_state_panel_reducer.handlers.handler_work_state_panel_reducer import (
    HandlerWorkStatePanelReducer,
)
from omnimarket.nodes.node_work_state_panel_reducer.state_codec import (
    decode,
    encode,
)
from tests.nodes.node_work_state_panel_reducer.harness import (
    StateIoRowStore,
    run_event,
)
from tests.nodes.node_work_state_panel_reducer.support import (
    NOW,
    event,
    overlay,
)

pytestmark = pytest.mark.unit

CONTRACT = Path(handler_module.__file__).resolve().parent.parent / "contract.yaml"
DRIVERS = ("asyncpg", "psycopg", "psycopg2", "sqlalchemy", "psycopg_pool")


def make_handler() -> HandlerWorkStatePanelReducer:
    return HandlerWorkStatePanelReducer(clock=lambda: NOW, overlay=overlay())


def held_row() -> ModelWorkStatePanelRow:
    store = StateIoRowStore()
    run_event(store, make_handler(), event(100))
    return decode(store.rows["file_sources"][0])


def problems_codec_roundtrip(
    row: ModelWorkStatePanelRow, wire: dict[str, Any]
) -> list[str]:
    problems: list[str] = []
    payload = json.loads(encode(row))
    for key in ("state", "in_flight", "tenant_id"):
        if key not in payload:
            problems.append(f"encode does not carry {key}")
    if decode(encode(row)) != row:
        problems.append("decode(encode(row)) differs from the row")
    key = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["state_io"]["key"]
    if wire.get(key) != "file_sources":
        problems.append(f"the wire dict has no {key!r} key the runtime can read")
    return problems


def test_state_io_codec_roundtrip() -> None:
    wire = event(100).model_dump(mode="json")
    assert problems_codec_roundtrip(held_row(), wire) == []


def test_control_wire_key_renamed() -> None:
    """Injected break `projector_renamed_on_the_wire`."""
    wire = event(100).model_dump(mode="json")
    wire["projectorx"] = wire.pop("projector")
    assert problems_codec_roundtrip(held_row(), wire) != []


def problems_no_database(source_by_module: dict[str, str]) -> list[str]:
    problems: list[str] = []
    params = inspect.signature(HandlerWorkStatePanelReducer.__init__).parameters
    for name in params:
        if any(word in name for word in ("db", "pool", "conn", "database", "session")):
            problems.append(
                f"constructor parameter {name!r} looks like a database handle"
            )
    for module, source in source_by_module.items():
        for node in ast.walk(ast.parse(source)):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in DRIVERS:
                    problems.append(f"{module} imports {name}")
    return problems


def test_reducer_reads_no_database() -> None:
    sources = {
        m.__name__: inspect.getsource(m)
        for m in (handler_module, service_module, state_codec)
    }
    assert problems_no_database(sources) == []
    store = StateIoRowStore()
    make_handler().handle(event(100))  # the ContextVar is unbound: a pure preview
    assert store.rows == {}


def test_control_a_variant_importing_the_driver() -> None:
    """Injected break `imports_asyncpg`."""
    assert problems_no_database({"variant": "import asyncpg\n"}) != []


def problems_restart_epoch(handler_after_restart: Any) -> list[str]:
    store = StateIoRowStore()
    run_event(store, make_handler(), event(100))
    run_event(store, make_handler(), event(200))
    restarted = StateIoRowStore()
    restarted.rows = dict(store.rows)
    run_event(restarted, handler_after_restart, event(300))
    epoch = decode(restarted.rows["file_sources"][0]).epoch
    return [] if epoch == 3 else [f"epoch after restart is {epoch}, not 3"]


def test_restart_epoch() -> None:
    assert problems_restart_epoch(make_handler()) == []


def test_control_handler_caching_the_epoch_in_memory() -> None:
    """Injected break `epoch_cached_in_instance_memory`: the restarted handler counts from 1."""

    class Caching(HandlerWorkStatePanelReducer):
        def handle(self, request: Any) -> dict[str, Any]:
            result = super().handle(request)
            proxy = state_codec.get_default_proxy()
            row = proxy["file_sources"]
            proxy["file_sources"] = row.model_copy(update={"epoch": 1})
            return result

    handler = Caching(clock=lambda: NOW, overlay=overlay())
    assert problems_restart_epoch(handler) != []


def test_handler_resolves(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    handler = HandlerWorkStatePanelReducer()
    assert handler.handler_type == "NODE_HANDLER"
    assert handler.handler_category == "COMPUTE"
    path = tmp_path / "overlay.yaml"
    path.write_text(
        yaml.safe_dump(json.loads(overlay().model_dump_json())), encoding="utf-8"
    )
    monkeypatch.setenv("ONEX_WORK_STATE_PANEL_OVERLAY", str(path))
    store = StateIoRowStore()
    run_event(store, handler, event(100))
    assert decode(store.rows["file_sources"][0]).epoch == 1


def problems_file_reads(run: Any) -> list[str]:
    opened: list[str] = []
    real_open = builtins.open

    def recording(file: Any, *a: Any, **k: Any) -> Any:
        opened.append(str(file))
        return real_open(file, *a, **k)

    builtins.open = recording
    original_read_text = Path.read_text

    def read_text(self: Path, *a: Any, **k: Any) -> str:
        opened.append(str(self))
        return original_read_text(self, *a, **k)

    Path.read_text = read_text
    try:
        run()
    finally:
        builtins.open = real_open
        Path.read_text = original_read_text
    outside = [p for p in opened if Path(p).name != "contract.yaml"]
    return [f"opened {p}" for p in outside]


def test_no_file_read() -> None:
    def golden() -> None:
        run_event(StateIoRowStore(), make_handler(), event(100))

    assert problems_file_reads(golden) == []


def test_control_a_variant_that_opens_another_file(tmp_path: Path) -> None:
    """Injected break `opens_a_state_file`."""
    extra = tmp_path / "other_state.md"
    extra.write_text("x", encoding="utf-8")

    def breaking() -> None:
        run_event(StateIoRowStore(), make_handler(), event(100))
        extra.read_text(encoding="utf-8")

    assert problems_file_reads(breaking) != []


def test_key_mismatch_writes_nothing() -> None:
    from omnimarket.models.work_state_projection.model_work_state_projected_event import (
        EnumWorkStateProjectorProfile,
    )

    store = StateIoRowStore()
    run_event(
        store,
        make_handler(),
        event(100, projector=EnumWorkStateProjectorProfile.UNREAD),
    )
    assert store.rows == {}
