# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23606: the row source of every ledger read is the deployment overlay's, never the contract's.

On a host whose ledger record is the bus, the reader's default source is the bus read. The ledger node refuses an answer over its
917,516-byte bound (the 14-day window is 222 MB, three of the four unresolved reads 8.7 to 55.8 MB) after loading the whole table,
and the reader then falls back to the file. Measured 2026-11-10 on lab host h202, one unresolved read of 5,593 rows: 35.5 s by
default, 9.5 s with ``--source file``. Which source answers inside the time limit is a fact of the host, so each node reads it from
its own overlay: the floor effect from ``node_factory_floor_effect`` (absent, the reader keeps its default), the unresolved step
from ``node_factory_floor_unresolved_effect`` (required). The failing control is a reader that refuses what the default does.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.model_merged_running_count import ModelMrcEvidenceRead
from omnimarket.nodes.node_factory_floor_effect.handlers.handler_factory_floor_effect import (
    HandlerFactoryFloorEffect,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_run import (
    ModelFactoryFloorRunRequest,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    OWN_CONTRACT_PATH,
    SourceRefusedError,
    read_ledger_rows,
    read_unresolved_rows,
)
from omnimarket.nodes.node_factory_floor_unresolved_effect.handlers import (
    HandlerFactoryFloorUnresolved,
)
from omnimarket.nodes.node_factory_floor_unresolved_effect.models.model_factory_floor_unresolved import (
    ModelFactoryFloorUnresolvedOverlay,
    ModelFactoryFloorUnresolvedRequest,
)

from .test_line_board_facts import EVIDENCE, OVERLAY, WINDOW_ROWS, _prepare, _Reader
from .test_runtime_factory_floor import NOW

pytestmark = pytest.mark.unit

ROW = "L1 2026-01-01T00:00:00Z | TERMINAL | lane=x | outcome=blocked | ticket=OMN-1 | text"


def _reader(tmp_path: Path, rows: str = ROW) -> tuple[Path, Path]:
    """A reader that records its arguments and, like the reader on a bus-record host, answers only when the file is named."""
    plugin = tmp_path / "plugin"
    (plugin / "scripts").mkdir(parents=True)
    log = tmp_path / "argv.jsonl"
    body = (
        "import json, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "args = sys.argv[1:]\n"
        "named = args[args.index('--source') + 1] if '--source' in args else None\n"
        "if named != 'file':\n"
        "    sys.stderr.write('bus read refused: answer over bound')\n"
        "    sys.exit(1)\n"
        f"print({rows!r})\n"
    )
    (plugin / "scripts" / "onex_ledger.py").write_text(body, encoding="utf-8")
    return plugin, log


def _argvs(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def _named(argv: list[str]) -> str | None:
    return argv[argv.index("--source") + 1] if "--source" in argv else None


def _window(plugin: Path, source: str | None) -> int:
    return len(
        read_ledger_rows(
            window_days=14,
            cut_chars=1200,
            timeout_seconds=60,
            source=source,
            plugin_root=plugin,
            plugin_root_env="CLAUDE_PLUGIN_ROOT",
            ledger_rows_file=None,
        )
    )


def test_the_floor_contract_names_no_row_source() -> None:
    """The break this change removes: a source written into the contract is the same on every host."""
    runtime = yaml.safe_load(OWN_CONTRACT_PATH.read_text(encoding="utf-8"))["runtime"]
    assert "ledger_source" not in runtime


def test_golden_the_window_read_names_the_source_it_is_given(tmp_path: Path) -> None:
    plugin, log = _reader(tmp_path)
    assert _window(plugin, "file") == 1
    (argv,) = _argvs(log)
    assert _named(argv) == "file"


def test_a_window_read_given_no_source_leaves_the_reader_its_default(
    tmp_path: Path,
) -> None:
    """No source named is no ``--source`` argument at all, and this reader's default is the refused one: a named refusal."""
    plugin, log = _reader(tmp_path)
    with pytest.raises(SourceRefusedError, match="answer over bound"):
        _window(plugin, None)
    (argv,) = _argvs(log)
    assert "--source" not in argv


def test_error_a_source_the_reader_refuses_is_a_named_refusal(tmp_path: Path) -> None:
    """The injected break: the bus source is refused by the reader, and the loader raises with the reader's own words, never an empty window."""
    plugin, _ = _reader(tmp_path)
    with pytest.raises(SourceRefusedError, match="answer over bound"):
        _window(plugin, "db")


def test_golden_every_unresolved_read_names_the_source_it_is_given(
    tmp_path: Path,
) -> None:
    plugin, log = _reader(tmp_path)
    reads = (("TERMINAL", "outcome=blocked"), ("STATUS", "handed-off="))
    read_unresolved_rows(
        reads=reads,
        cut_chars=1200,
        timeout_seconds=60,
        source="file",
        plugin_root=plugin,
        plugin_root_env="CLAUDE_PLUGIN_ROOT",
        stand_in_file=None,
        kinds=frozenset({"TERMINAL"}),
        older_than=datetime(2027, 1, 1, tzinfo=UTC),
        max_workers=2,
    )
    argvs = _argvs(log)
    assert len(argvs) == len(reads)
    assert all(_named(argv) == "file" for argv in argvs)


def _step_overlay(**changes: object) -> dict[str, object]:
    return {
        "reads": [{"kind": "TERMINAL", "grep": "outcome=blocked"}],
        "ledger_source": "file",
        "read_workers": 1,
        "read_timeout_seconds": 60,
        "overlap_hours": 24,
        **changes,
    }


def test_the_unresolved_step_names_its_overlays_source(tmp_path: Path) -> None:
    plugin, log = _reader(tmp_path)
    overlay = ModelFactoryFloorUnresolvedOverlay.model_validate(_step_overlay())
    record = HandlerFactoryFloorUnresolved(overlay, clock=lambda: NOW).handle(
        ModelFactoryFloorUnresolvedRequest(read_at=NOW, plugin_root=str(plugin))
    )
    assert [row.lane for row in record.rows] == ["x"]
    assert [_named(argv) for argv in _argvs(log)] == ["file"]


def test_error_the_unresolved_step_is_refused_on_the_source_its_overlay_names(
    tmp_path: Path,
) -> None:
    """The source is the overlay's and nothing else's: an overlay that names the bus read gets the reader's refusal."""
    plugin, log = _reader(tmp_path)
    overlay = ModelFactoryFloorUnresolvedOverlay.model_validate(
        _step_overlay(ledger_source="db")
    )
    with pytest.raises(SourceRefusedError, match="answer over bound"):
        HandlerFactoryFloorUnresolved(overlay, clock=lambda: NOW).handle(
            ModelFactoryFloorUnresolvedRequest(read_at=NOW, plugin_root=str(plugin))
        )
    assert [_named(argv) for argv in _argvs(log)] == ["db"]


def test_the_unresolved_overlay_is_refused_without_a_source() -> None:
    document = _step_overlay()
    del document["ledger_source"]
    with pytest.raises(ValueError, match="ledger_source"):
        ModelFactoryFloorUnresolvedOverlay.model_validate(document)


def _run_effect(
    world: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    overlay: dict[str, object],
) -> tuple[list[list[str]], dict[str, Any]]:
    """The floor effect over the recorded world with its window read left to the reader, under ``overlay``."""
    files = _prepare(world, monkeypatch)
    files["overlay"].write_text(yaml.safe_dump(overlay), encoding="utf-8")
    plugin, log = _reader(world["tmp"], WINDOW_ROWS)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin))
    result = HandlerFactoryFloorEffect(merged_running=_Reader(EVIDENCE)).handle(
        ModelFactoryFloorRunRequest(
            now=NOW,
            home=str(world["home"]),
            kb=str(world["kb"]),
            unresolved_rows_file=str(files["archive"]),
        )
    )
    return _argvs(log), json.loads(Path(result.output_path).read_text())


def test_golden_the_floor_effect_names_its_overlays_source_to_the_reader(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    argvs, record = _run_effect(
        world, monkeypatch, {**OVERLAY, "ledger_source": "file"}
    )
    assert [_named(argv) for argv in argvs] == ["file"]
    health = record["sources"]["health"]
    ledger = next(entry for entry in health if entry["source"] == "ledger")
    assert ledger["state"] == "read", ledger


def test_error_the_floor_effect_with_no_overlay_source_is_refused_by_this_reader(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failing control for the overlay key: without it the reader is told no source, and this reader refuses its default."""
    with pytest.raises(SourceRefusedError, match="answer over bound"):
        _run_effect(world, monkeypatch, dict(OVERLAY))


class _MarkingReader(_Reader):
    """The merged-and-running reader, leaving a file the moment its evidence is read."""

    def __init__(self, read: ModelMrcEvidenceRead, marker: Path) -> None:
        super().__init__(read)
        self._marker = marker

    def read_evidence(self, now: datetime) -> ModelMrcEvidenceRead:
        self._marker.write_text("read", encoding="utf-8")
        return super().read_evidence(now)


def _waiting_reader(tmp_path: Path, marker: Path, wait_seconds: float) -> Path:
    """A ledger reader that answers only once the running evidence was read, and refuses when it waited in vain."""
    plugin = tmp_path / "waiting-plugin"
    (plugin / "scripts").mkdir(parents=True)
    (plugin / "scripts" / "onex_ledger.py").write_text(
        "import os, sys, time\n"
        f"limit = time.monotonic() + {wait_seconds!r}\n"
        f"while not os.path.exists({str(marker)!r}):\n"
        "    if time.monotonic() > limit:\n"
        "        sys.stderr.write('no other read ran while the window read did')\n"
        "        sys.exit(1)\n"
        "    time.sleep(0.05)\n"
        f"print({WINDOW_ROWS!r})\n",
        encoding="utf-8",
    )
    return plugin


def _run_beside(
    world: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    wait_seconds: float,
    awaited: str = "evidence-read",
) -> None:
    """Run the effect with a reader that waits for the file ``awaited``; the evidence read leaves the file ``evidence-read``."""
    files = _prepare(world, monkeypatch)
    files["overlay"].write_text(
        yaml.safe_dump({**OVERLAY, "ledger_source": "file"}), encoding="utf-8"
    )
    marker = world["tmp"] / "evidence-read"
    plugin = _waiting_reader(world["tmp"], world["tmp"] / awaited, wait_seconds)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin))
    HandlerFactoryFloorEffect(merged_running=_MarkingReader(EVIDENCE, marker)).handle(
        ModelFactoryFloorRunRequest(
            now=NOW,
            home=str(world["home"]),
            kb=str(world["kb"]),
            unresolved_rows_file=str(files["archive"]),
        )
    )


def test_the_window_read_runs_beside_the_steps_other_reads(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured on the publishing Mac 2026-11-10: the window read was 14.7 s of the step's 24.1 s while every other read waited for it.

    The reader here answers only once the running evidence was read. Break: a handler that waits for the window read before
    its other reads (the code before this change) never reads the evidence while the reader waits, so the reader refuses.
    """
    _run_beside(world, monkeypatch, wait_seconds=60.0)


def test_error_a_window_read_refused_beside_the_other_reads_is_still_the_steps_refusal(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reader's refusal is raised from the worker thread to the step, naming the reader's words, never an empty window."""
    with pytest.raises(SourceRefusedError, match="no other read ran"):
        _run_beside(world, monkeypatch, wait_seconds=0.0, awaited="never-written")
