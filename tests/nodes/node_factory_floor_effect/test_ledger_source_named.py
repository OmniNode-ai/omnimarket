# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23606: the row source of the unresolved step's reads is its overlay's, never the contract's; the floor's window is the read node's (OMN-23669).

On a host whose ledger record is the bus, the reader's default source is the bus read. The ledger node refuses an answer over its
917,516-byte bound (the 14-day window is 222 MB, three of the four unresolved reads 8.7 to 55.8 MB) after loading the whole table,
and the reader then falls back to the file. Measured 2026-11-10 on lab host h202, one unresolved read of 5,593 rows: 35.5 s by
default, 9.5 s with ``--source file``. The unresolved step (a later slice of the board's move to projections) reads its source from
``node_factory_floor_unresolved_effect`` (required). The floor effect no longer calls the reader: its window comes from the read
node's pages, so no row source is named in its overlay (OMN-23669). The failing control is a reader that refuses what the default does.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_merged_running_count import ModelMrcEvidenceRead
from omnimarket.nodes.node_factory_floor_effect.handlers.handler_factory_floor_effect import (
    HandlerFactoryFloorEffect,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_run import (
    ModelFactoryFloorRunRequest,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_runtime import (
    ModelFactoryFloorOverlay,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    OWN_CONTRACT_PATH,
    SourceRefusedError,
    read_unresolved_rows,
)
from omnimarket.nodes.node_factory_floor_unresolved_effect.handlers import (
    HandlerFactoryFloorUnresolved,
)
from omnimarket.nodes.node_factory_floor_unresolved_effect.models.model_factory_floor_unresolved import (
    ModelFactoryFloorUnresolvedOverlay,
    ModelFactoryFloorUnresolvedRequest,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    ModelPointAnswer,
    ModelPointQuery,
)

from .test_line_board_facts import EVIDENCE, OVERLAY, WINDOW_ROWS, _prepare, _Reader
from .test_runtime_factory_floor import NOW
from .window_port import RecordedWindowPort

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


def test_the_floor_contract_names_no_row_source() -> None:
    """The break this change removes: a source written into the contract is the same on every host."""
    runtime = yaml.safe_load(OWN_CONTRACT_PATH.read_text(encoding="utf-8"))["runtime"]
    assert "ledger_source" not in runtime


def test_error_the_floor_overlay_takes_no_row_source() -> None:
    """The file value is gone: an overlay that still names a row source is refused by name, never read past."""
    with pytest.raises(ValueError, match="ledger_source"):
        ModelFactoryFloorOverlay.model_validate({**OVERLAY, "ledger_source": "file"})


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


class _MarkingReader(_Reader):
    """The merged-and-running reader, leaving a file the moment its evidence is read."""

    def __init__(self, read: ModelMrcEvidenceRead, marker: Path) -> None:
        super().__init__(read)
        self._marker = marker

    def read_evidence(
        self, now: datetime, *, hosts_table: Path | None = None
    ) -> ModelMrcEvidenceRead:
        self._marker.write_text("read", encoding="utf-8")
        return super().read_evidence(now, hosts_table=hosts_table)


class _WaitingPort(RecordedWindowPort):
    """A read port that answers only once the running evidence was read, and refuses when it waited in vain."""

    def __init__(self, text: str, awaited: Path, wait_seconds: float) -> None:
        super().__init__(text)
        self._awaited = awaited
        self._wait = wait_seconds

    def read(self, query: ModelPointQuery) -> ModelPointAnswer:
        limit = time.monotonic() + self._wait
        while not self._awaited.exists():
            if time.monotonic() > limit:
                return ModelPointAnswer(
                    error="no other read ran while the window read did"
                )
            time.sleep(0.05)
        return super().read(query)


def _run_beside(
    world: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    wait_seconds: float,
    awaited: str = "evidence-read",
) -> None:
    """Run the effect with a read port that waits for the file ``awaited``; the evidence read leaves the file ``evidence-read``."""
    files = _prepare(world, monkeypatch)
    marker = world["tmp"] / "evidence-read"
    port = _WaitingPort(WINDOW_ROWS, world["tmp"] / awaited, wait_seconds)
    HandlerFactoryFloorEffect(
        merged_running=_MarkingReader(EVIDENCE, marker), ledger_source=port
    ).handle(
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

    The port here answers only once the running evidence was read. Break: a handler that waits for the window read before
    its other reads never reads the evidence while the port waits, so the port refuses.
    """
    _run_beside(world, monkeypatch, wait_seconds=60.0)


def test_error_a_window_read_refused_beside_the_other_reads_is_still_the_steps_refusal(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The port's refusal is raised from the worker thread to the step, naming the read as unread, never an empty window."""
    with pytest.raises(SourceRefusedError, match="no other read ran"):
        _run_beside(world, monkeypatch, wait_seconds=0.0, awaited="never-written")
