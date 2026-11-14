# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998, ledger cutover Wave 0: the projection window is the file window, and it opens no file.

Golden parity: the same rows, once parsed from a file answer (``L<n> `` prefixes, the contract's kinds)
and once read through the work ledger projection (bare lines), give the same tuple of rows. Golden step: an
overlay that names ``ledger_projection`` runs the floor over the projection alone -- the ledger file, its split
archives and the reader subprocess are guarded, and the guard stays empty. The failing control is the same run
without the projection key: the file window read it replaces trips the guard and is refused by the refusing
reader. An overlay that names the projection with no reader bound is refused, never answered from a file.
"""

from __future__ import annotations

import builtins
import json
import pathlib
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.model_factory_floor_input import ModelFactoryFloorRequest
from omnimarket.nodes.node_factory_floor_compute.handlers import handler_factory_floor
from omnimarket.nodes.node_factory_floor_effect.handlers import (
    handler_factory_floor_effect as effect_module,
)
from omnimarket.nodes.node_factory_floor_effect.handlers.handler_factory_floor_effect import (
    HandlerFactoryFloorEffect,
)
from omnimarket.nodes.node_factory_floor_effect.handlers.ledger_window_from_projection import (
    read_ledger_window_from_projection,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_run import (
    ModelFactoryFloorRunRequest,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    SourceRefusedError,
    contract_ledger_kinds,
    load_contract,
    parse_ledger_text,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters import (
    InMemoryEventSink,
    InMemoryPointRead,
)
from omnimarket.nodes.node_work_ledger_reader_effect.handlers import (
    HandlerWorkLedgerReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    ModelInMemoryReadRoute,
    ModelPointAnswer,
    ModelReaderEvents,
    ModelReaderOverlay,
    ModelReaderParity,
)

from .test_line_board_facts import EVIDENCE, WINDOW_ROWS, _prepare, _Reader
from .test_runtime_factory_floor import CONTRACT, NOW

pytestmark = pytest.mark.unit

READER = "factory-floor"
# The directory the test's stand-in ledger record lives in; the guard watches every access under it. The env variable below is one the
# test's own file overlay could name; the projection step never reads it.
RECORD_DIR = "ledger-record"
ARCHIVE_PATH_ENV = "FLOOR_TEST_LEDGER_RECORD"
# The recorded rows are minutes older than NOW, so the window's start drops none of them; each row lands once, in the
# chunk of its own UTC day, though the in-memory reader answers the same lines for every day it is asked.
WINDOW_DAYS = 14


def _bare_lines() -> tuple[str, ...]:
    """The projection's form of the window rows: the same rows with no ``L<n> `` prefix."""
    return tuple(re.sub(r"^L\d+ ", "", line) for line in WINDOW_ROWS.splitlines())


def _projection_handler() -> HandlerWorkLedgerReader:
    """A moved work ledger reader answering the rows window from memory, as fresh as NOW."""
    lines = _bare_lines()
    answer = ModelPointAnswer(lines=lines, matched=len(lines), watermark=NOW)
    return HandlerWorkLedgerReader(
        ModelReaderOverlay(
            moved_readers=(READER,),
            max_watermark_age_seconds=900,
            page_size=1000,
            max_pages=10,
            rows_lines_only=True,
            route=ModelInMemoryReadRoute(kind="inmemory"),
            parity=ModelReaderParity(
                receipt_lane="work-ledger-parity", window_hours=48
            ),
            events=ModelReaderEvents(journal_env="X_JOURNAL"),
        ),
        InMemoryPointRead({"rows": answer}),
        InMemoryEventSink(),
        clock=lambda: NOW,
    )


def test_parity_file_window_and_projection_window_give_the_same_rows() -> None:
    kinds = contract_ledger_kinds()
    from_file = parse_ledger_text(WINDOW_ROWS, kinds)
    from_projection = read_ledger_window_from_projection(
        reader=READER,
        read_factory=lambda: _projection_handler().handle,
        window_days=WINDOW_DAYS,
        now=NOW,
        kinds=kinds,
        cache=None,
        cache_ttl_seconds=900,
        read_workers=None,
        settle_seconds=3600,
    )
    assert {row.kind for row in from_file} == {
        "CLAIM",
        "RULING",
        "FRICTION",
        "RELEASE",
        "CORRECTION",
        "ACK",
        "MSG",
    }
    assert from_projection == from_file


def _name_projection(files: dict[str, Path]) -> None:
    """Rewrite the overlay the preparation wrote, naming the projection as the ledger's record."""
    document = yaml.safe_load(files["overlay"].read_text(encoding="utf-8"))
    files["overlay"].write_text(
        yaml.safe_dump(
            {
                **document,
                "ledger_projection": {
                    "reader": READER,
                    "cache_ttl_seconds": 900,
                    "settle_seconds": 3600,
                },
            }
        ),
        encoding="utf-8",
    )


def _file_ledger_env(world: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """The ledger file and one split archive the env names, and a plugin root whose reader refuses every query."""
    live = world["tmp"] / RECORD_DIR / "record.md"
    archive = live.parent / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    live.write_text(WINDOW_ROWS + "\n", encoding="utf-8")
    (archive / "record_2026-01-01-split.md").write_text(
        "L99 2026-01-01T00:00:00Z | STATUS | lane=old | ticket=OMN-9 | an archived row\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(ARCHIVE_PATH_ENV, str(live))
    plugin = world["tmp"] / "refusing-plugin"
    (plugin / "scripts").mkdir(parents=True, exist_ok=True)
    (plugin / "scripts" / "onex_ledger.py").write_text(
        "import sys\nsys.stderr.write('the ledger file was read')\nsys.exit(1)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(load_contract(CONTRACT).runtime.plugin_root_env, str(plugin))


def _ledger_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[str], list[list[str]]]:
    """Every access to the ledger file (open, Path.open, Path.read_text) and every onex_ledger subprocess call."""
    accesses: list[str] = []
    calls: list[list[str]] = []
    real_open = builtins.open
    real_path_open = pathlib.Path.open
    real_read_text = pathlib.Path.read_text
    real_run = subprocess.run

    def recording_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if RECORD_DIR in str(file):
            accesses.append(str(file))
        return real_open(file, *args, **kwargs)

    def recording_path_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if RECORD_DIR in str(self):
            accesses.append(str(self))
        return real_path_open(self, *args, **kwargs)

    def recording_read_text(self: Path, *args: Any, **kwargs: Any) -> Any:
        if RECORD_DIR in str(self):
            accesses.append(str(self))
        return real_read_text(self, *args, **kwargs)

    def recording_run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        parts = [str(part) for part in argv]
        if "onex_ledger" in " ".join(parts):
            calls.append(parts)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", recording_open)
    monkeypatch.setattr(pathlib.Path, "open", recording_path_open)
    monkeypatch.setattr(pathlib.Path, "read_text", recording_read_text)
    monkeypatch.setattr(subprocess, "run", recording_run)
    return accesses, calls


def _run_step(
    world: dict[str, Path],
    files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    projection_read: Any = None,
) -> tuple[ModelFactoryFloorRequest, dict[str, Any]]:
    """Run the effect without ledger_rows_file and return the request the compute received and the record written."""
    seen: list[ModelFactoryFloorRequest] = []

    class Recording(handler_factory_floor.HandlerFactoryFloor):
        def handle(self, request: ModelFactoryFloorRequest) -> Any:
            seen.append(request)
            return super().handle(request)

    monkeypatch.setattr(effect_module, "HandlerFactoryFloor", Recording)
    handler = HandlerFactoryFloorEffect(
        merged_running=_Reader(EVIDENCE), ledger_projection_read=projection_read
    )
    result = handler.handle(
        ModelFactoryFloorRunRequest(
            now=NOW,
            home=str(world["home"]),
            kb=str(world["kb"]),
            unresolved_rows_file=str(files["archive"]),
        )
    )
    return seen[0], json.loads(Path(result.output_path).read_text(encoding="utf-8"))


def test_golden_step_reads_no_ledger_file_when_the_overlay_names_the_projection(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _prepare(world, monkeypatch)
    _name_projection(files)
    _file_ledger_env(world, monkeypatch)
    projection = _projection_handler()
    accesses, calls = _ledger_guard(monkeypatch)
    request, record = _run_step(
        world, files, monkeypatch, projection_read=lambda: projection.handle
    )
    # The projection's rows reached the compute and the record: the ledger reads as read.
    ruling = next(row for row in request.ledger if row.kind == "RULING")
    assert ruling.mechanism == "OMN-23025"
    health = record["sources"]["health"]
    ledger = next(entry for entry in health if entry["source"] == "ledger")
    assert ledger["state"] == "read", ledger
    assert accesses == []
    assert calls == []


def test_failing_control_the_file_overlay_trips_the_zero_read_guard(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the projection key the window is the file read it replaces: the refusing reader makes the
    step refuse, and the guard sees the subprocess call -- so the empty guard above can fail."""
    files = _prepare(world, monkeypatch)
    _file_ledger_env(world, monkeypatch)
    accesses, calls = _ledger_guard(monkeypatch)
    with pytest.raises(SourceRefusedError, match="the ledger file was read"):
        _run_step(world, files, monkeypatch)
    assert calls or accesses


def test_error_projection_named_but_no_reader_bound_is_refused(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The projection named and no reader bound is a refusal naming the fix; the file is never read in its place."""
    files = _prepare(world, monkeypatch)
    _name_projection(files)
    _file_ledger_env(world, monkeypatch)
    accesses, calls = _ledger_guard(monkeypatch)
    with pytest.raises(SourceRefusedError, match="no work ledger reader is bound"):
        _run_step(world, files, monkeypatch)
    assert accesses == []
    assert calls == []
