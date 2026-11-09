# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Placement of the snapshot job and restore drill is checked against the machine they run on (OMN-23070).

Tessl finding on the merged snapshot pull request: the same-host and off-source checks compared two overlay strings,
while the store writes through the local filesystem and the drill probes from the local network, so a run on the source
machine with distinct labels still passed. Each test failed on dev before the fix.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_work_ledger_copy_effect import runtime_work_ledger_copy
from omnimarket.nodes.node_work_ledger_copy_effect.adapters.adapters_snapshot import (
    MemorySnapshotStore,
)
from omnimarket.nodes.node_work_ledger_copy_effect.container.container_work_ledger_copy_effect import (
    create_handler_work_ledger_restore_drill,
    create_handler_work_ledger_snapshot,
)
from omnimarket.nodes.node_work_ledger_copy_effect.models import (
    EnumDrillOutcome,
    EnumSnapshotOutcome,
    ModelSnapshotOverlay,
)
from tests.unit.nodes.node_work_ledger_copy_effect.support_snapshot import (
    DRILL_HOST,
    SOURCE_HOST,
    RecordingRunner,
    drill_request,
    rows,
    snapshot_handler,
    snapshot_request,
)

pytestmark = pytest.mark.unit
_APPEND = "/usr/local/bin/example-bus-append"
_ENVIRONMENT = {"ONEX_LEDGER_BUS_APPEND_COMMAND": f"{_APPEND} --bus-lane dev"}
_EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_work_ledger_copy_effect/overlay.snapshot.example.yaml"
)


def _overlay(tmp_path: Path) -> ModelSnapshotOverlay:
    raw = yaml.safe_load(_EXAMPLE.read_text())
    raw["store"]["directory"] = str(tmp_path / "snapshots")
    raw["store"]["host"] = "copy-host.example.invalid"
    raw["drill"]["host"] = "copy-host.example.invalid"
    return ModelSnapshotOverlay.model_validate(raw)


def _export() -> str:
    return "".join(row.model_dump_json() + "\n" for row in rows(4))


def _runner() -> RecordingRunner:
    return RecordingRunner(
        {
            "/usr/local/bin/example-export": (0, _export()),
            "nc": (1, ""),
            "onex-ledger": (0, ""),
        }
    )


def _on(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setattr(socket, "gethostname", lambda: name)


def test_a_snapshot_run_on_the_source_machine_is_refused_though_the_labels_differ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = _overlay(tmp_path)
    _on(monkeypatch, overlay.source.host)
    runner = _runner()
    result = create_handler_work_ledger_snapshot(
        overlay, _ENVIRONMENT, run=runner
    ).handle(snapshot_request())
    assert not result.ok
    assert runner.commands == []
    assert not (tmp_path / "snapshots").exists()
    assert result.outcome is EnumSnapshotOutcome.REFUSED_NOT_STORE_HOST


def test_a_snapshot_run_on_the_store_machine_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = _overlay(tmp_path)
    _on(monkeypatch, "COPY-HOST.example.invalid")
    result = create_handler_work_ledger_snapshot(
        overlay, _ENVIRONMENT, run=_runner()
    ).handle(snapshot_request())
    assert result.outcome is EnumSnapshotOutcome.WRITTEN


def test_a_drill_run_on_the_source_machine_is_refused_before_any_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = _overlay(tmp_path)
    _on(monkeypatch, overlay.source.host)
    runner = _runner()
    result = create_handler_work_ledger_restore_drill(
        overlay, _ENVIRONMENT, run=runner
    ).handle(drill_request(None))
    assert [a[0] for a in runner.commands if a[0] not in ("onex-ledger", _APPEND)] == []
    assert result.outcome is EnumDrillOutcome.REFUSED_NOT_DRILL_HOST


def test_a_drill_run_on_the_drill_machine_goes_on_to_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = _overlay(tmp_path)
    _on(monkeypatch, "copy-host")
    runner = _runner()
    result = create_handler_work_ledger_restore_drill(
        overlay, _ENVIRONMENT, run=runner
    ).handle(drill_request(None))
    assert result.outcome is EnumDrillOutcome.REFUSED_NO_SNAPSHOT
    assert [a[0] for a in runner.commands if a[0] not in ("onex-ledger", _APPEND)] == [
        "nc"
    ]


@pytest.mark.parametrize(
    "spelled", ["SOURCE.EXAMPLE.INVALID", "source.example.invalid."]
)
def test_the_source_host_spelled_another_way_is_still_the_source_host(
    spelled: str,
) -> None:
    result = snapshot_handler(MemorySnapshotStore(), store_host=spelled).handle(
        snapshot_request()
    )
    assert result.outcome is EnumSnapshotOutcome.REFUSED_SAME_HOST
    assert SOURCE_HOST.lower() == "source.example.invalid"


def test_a_machine_that_is_not_the_store_host_is_refused() -> None:
    result = snapshot_handler(
        MemorySnapshotStore(), local_host="elsewhere.example.invalid"
    ).handle(snapshot_request())
    assert result.outcome is EnumSnapshotOutcome.REFUSED_NOT_STORE_HOST
    assert DRILL_HOST != "elsewhere.example.invalid"


def test_the_command_line_prints_a_prune_warning_beside_the_written_result(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from omnimarket.nodes.node_work_ledger_copy_effect.protocols import CopyStepError

    class PruneFails(MemorySnapshotStore):
        def prune(self, keep: int) -> tuple[str, ...]:
            raise CopyStepError("store", "could not prune 20250101T000000Z: busy")

    monkeypatch.setattr(
        runtime_work_ledger_copy, "load_snapshot_overlay", lambda: _overlay(tmp_path)
    )
    monkeypatch.setattr(
        runtime_work_ledger_copy,
        "create_handler_work_ledger_snapshot",
        lambda *_: snapshot_handler(PruneFails()),
    )
    code = runtime_work_ledger_copy._snapshot_verb("snapshot")
    out = capsys.readouterr().out
    assert code == 0
    assert "snapshot-written" in out
    assert "could not prune" in out
