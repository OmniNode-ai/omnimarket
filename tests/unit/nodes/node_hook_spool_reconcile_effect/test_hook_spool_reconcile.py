# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hook drainer's spool drains and its losses raise an alarm (OMN-19998, issue L1-06): golden, error, control and seam tests.

Measured on h053 on 2026-11-12: 118 records of one class (content.captured) sat in the drainer's quarantine, 587 more in a
directory set aside by hand, and the loss log held 118 lines that no alarm ever covered. Golden: dead letters of a class the
drainer no longer refuses are carried back to the journal, the spool reads empty, and the chain succeeds. Error: a class the
drainer still refuses is held and its loss raises one typed alarm, once; an unreadable drainer status carries nothing back.
Failing control: with the alarm dropped from the journal a dead letter with no alarm reads as not alarmed, the check the
golden run passes. Seam: the real files the drainer writes (a quarantine record and its reason, the status, a loss line)
are read as the node reads them.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.emission_sink import (
    InMemoryEmissionSink,
    JsonlEmissionSink,
)
from omnimarket.models.floor_17a import ModelNodeEmission
from omnimarket.nodes.node_hook_spool_reconcile_effect import (
    HandlerHookSpoolReconcile,
    decide_spool,
)
from omnimarket.nodes.node_hook_spool_reconcile_effect.handlers.handler_hook_spool_reconcile import (
    LOSS_FAILURE_CODE,
    STATUS_UNREAD_CODE,
)
from omnimarket.nodes.node_hook_spool_reconcile_effect.models import (
    ModelHookSpoolReconcileCommand,
    ModelSpoolRecord,
)
from omnimarket.nodes.node_hook_spool_reconcile_effect.overlay import (
    ModelHookSpoolReconcileOverlay,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 12, 16, 0, tzinfo=UTC)
HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
OVERLAY_EXAMPLE = (
    HERE.parents[3]
    / "src/omnimarket/nodes/node_hook_spool_reconcile_effect/overlay.example.yaml"
)


def _overlay() -> ModelHookSpoolReconcileOverlay:
    return ModelHookSpoolReconcileOverlay.model_validate(
        yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
    )


def _record(event_type: str, name: str) -> dict[str, object]:
    return {
        "event_id": name,
        "event_type": event_type,
        "payload": {},
        "queued_at": "2026-11-06T04:51:13+00:00",
    }


def _spool(
    tmp_path: Path,
    *,
    dead: dict[str, str] | None = None,
    denied: dict[str, str] | None = None,
    refused: list[str] | None = None,
    losses: int = 0,
    status: bool = True,
) -> Path:
    for name, event_type in (dead or {}).items():
        d = tmp_path / "hook_emit_journal/quarantine"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(json.dumps(_record(event_type, name)))
        (d / f"{Path(name).stem}.reason.json").write_text("{}")
    for name, event_type in (denied or {}).items():
        d = tmp_path / "hook_emit_journal_denied_classes"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(json.dumps(_record(event_type, name)))
    if status:
        (tmp_path / "hook_emit_drainer_status.json").write_text(
            json.dumps({"refused_event_types": refused or []})
        )
    (tmp_path / "hook_emit_journal_losses.jsonl").write_text(
        "".join('{"disposition": "dead-lettered"}\n' for _ in range(losses))
    )
    return tmp_path


def _handler(
    home: Path, sink: InMemoryEmissionSink | JsonlEmissionSink
) -> HandlerHookSpoolReconcile:
    return HandlerHookSpoolReconcile(
        overlay=_overlay(),
        home=home,
        sink=sink,
        backend="in_memory",
        clock=lambda: NOW,
    )


def _alarm_on_journal(events: list[ModelNodeEmission]) -> bool:
    """The check the board makes: a typed loss failure sits on the emission journal."""
    return any(
        e.phase == "failure" and e.failure_code == LOSS_FAILURE_CODE for e in events
    )


def _journal_files(home: Path) -> list[str]:
    d = home / "hook_emit_journal"
    return sorted(p.name for p in d.glob("*.json")) if d.is_dir() else []


# -- the pure decision --------------------------------------------------------------------------------------------------


def _rec(name: str, event_type: str, age: int = 100) -> ModelSpoolRecord:
    return ModelSpoolRecord(
        name=name, location="q", event_type=event_type, age_seconds=age
    )


def test_decide_is_pure_and_partitions() -> None:
    records = [
        _rec("b.json", "content.captured"),
        _rec("a.json", "content.captured"),
        _rec("c.json", "prompt.submitted"),
        _rec("d.json", "content.captured", age=1),
    ]
    first = decide_spool(records, ["prompt.submitted"], 1, 10, 5, 2)
    assert first == decide_spool(records, ["prompt.submitted"], 1, 10, 5, 2)
    assert [r.name for r in first.replay] == ["a.json"]
    assert (first.held_refused, first.held_young, first.unalarmed_losses) == (1, 1, 3)


# -- golden chain -------------------------------------------------------------------------------------------------------


def test_golden_dead_letters_drain_and_the_chain_succeeds(tmp_path: Path) -> None:
    home = _spool(
        tmp_path,
        dead={"01_a.json": "content.captured", "02_b.json": "content.captured"},
        denied={"03_c.json": "prompt.submitted"},
    )
    (home / "hook_emit_journal_losses.jsonl").write_text("")
    sink = InMemoryEmissionSink()
    result = _handler(home, sink).handle(ModelHookSpoolReconcileCommand(replay=True))
    # right after the carry the records wait in the journal for the drainer: draining, not yet drained
    assert result.status == "draining"
    assert result.ok
    assert (result.before.dead_lettered, result.after.dead_lettered) == (3, 0)
    assert (result.after.pending, result.replayed) == (3, 3)
    assert _journal_files(home) == ["01_a.json", "02_b.json", "03_c.json"]
    # the reason file is kept beside the record, never deleted
    assert (home / "hook_emit_journal/quarantine/replayed/01_a.reason.json").exists()
    assert [e.phase for e in sink.events] == ["start", "success"]
    assert sink.events[-1].counters["replayed"] == 3
    assert not _alarm_on_journal(sink.events)
    # the drainer publishes what the journal holds; the next run reads the spool empty
    for name in _journal_files(home):
        (home / "hook_emit_journal" / name).unlink()
    again = _handler(home, sink).handle(ModelHookSpoolReconcileCommand(replay=True))
    assert again.status == "drained"
    assert (again.after.pending, again.after.dead_lettered) == (0, 0)


def test_golden_batch_limit_drains_over_runs(tmp_path: Path) -> None:
    names = {f"{i:02d}.json": "content.captured" for i in range(5)}
    home = _spool(tmp_path, dead=names)
    (home / "hook_emit_journal_losses.jsonl").write_text("")
    overlay = _overlay().model_copy(
        update={"replay": _overlay().replay.model_copy(update={"batch_limit": 2})}
    )
    h = HandlerHookSpoolReconcile(
        overlay=overlay,
        home=home,
        sink=InMemoryEmissionSink(),
        backend="in_memory",
        clock=lambda: NOW,
    )
    first = h.handle(ModelHookSpoolReconcileCommand(replay=True))
    assert (first.status, first.replayed, first.after.dead_lettered) == (
        "draining",
        2,
        3,
    )
    h.handle(ModelHookSpoolReconcileCommand(replay=True))
    last = h.handle(ModelHookSpoolReconcileCommand(replay=True))
    assert last.after.dead_lettered == 0


# -- error chain --------------------------------------------------------------------------------------------------------


def test_error_loss_raises_one_alarm_once(tmp_path: Path) -> None:
    home = _spool(
        tmp_path,
        dead={"01_a.json": "content.captured"},
        refused=["content.captured"],
        losses=3,
    )
    sink = InMemoryEmissionSink()
    h = _handler(home, sink)
    first = h.handle(ModelHookSpoolReconcileCommand(replay=True))
    assert first.status == "stuck"
    assert first.held_refused == 1
    assert first.replayed == 0
    assert not first.ok
    assert first.alarmed_losses == 3
    assert [e.phase for e in sink.events] == ["start", "failure"]
    assert sink.events[-1].failure_code == LOSS_FAILURE_CODE
    assert "content.captured" in sink.events[-1].failure_detail
    assert _alarm_on_journal(sink.events)
    assert _journal_files(home) == []
    # no new loss: no second alarm, the held record still counted
    second = h.handle(ModelHookSpoolReconcileCommand(replay=True))
    assert second.ok
    assert second.alarmed_losses == 0
    assert sink.events[-1].phase == "success"
    # a new loss raises a new alarm
    (home / "hook_emit_journal_losses.jsonl").write_text('{"x": 1}\n' * 4)
    third = h.handle(ModelHookSpoolReconcileCommand(replay=True))
    assert third.alarmed_losses == 1


def test_error_unreadable_drainer_status_carries_nothing_back(tmp_path: Path) -> None:
    home = _spool(tmp_path, dead={"01_a.json": "content.captured"}, status=False)
    sink = InMemoryEmissionSink()
    result = _handler(home, sink).handle(ModelHookSpoolReconcileCommand(replay=True))
    assert result.status == "unread"
    assert not result.ok
    assert result.replayed == 0
    assert _journal_files(home) == []
    assert [e.failure_code for e in sink.events if e.phase == "failure"] == [
        STATUS_UNREAD_CODE
    ]


def test_error_unreadable_record_is_counted_and_never_moved(tmp_path: Path) -> None:
    home = _spool(tmp_path, dead={"01_a.json": "content.captured"})
    (home / "hook_emit_journal/quarantine/02_bad.json").write_text("{not json")
    result = _handler(home, InMemoryEmissionSink()).handle(
        ModelHookSpoolReconcileCommand(replay=True)
    )
    assert result.after.unreadable == 1
    assert (home / "hook_emit_journal/quarantine/02_bad.json").exists()
    assert _journal_files(home) == ["01_a.json"]


def test_error_a_record_already_in_the_journal_is_never_overwritten(
    tmp_path: Path,
) -> None:
    home = _spool(tmp_path, dead={"01_a.json": "content.captured"})
    (home / "hook_emit_journal").mkdir(exist_ok=True)
    (home / "hook_emit_journal/01_a.json").write_text('{"keep": true}')
    result = _handler(home, InMemoryEmissionSink()).handle(
        ModelHookSpoolReconcileCommand(replay=True)
    )
    assert result.replayed == 0
    assert json.loads((home / "hook_emit_journal/01_a.json").read_text()) == {
        "keep": True
    }


def test_probe_moves_nothing_and_still_alarms(tmp_path: Path) -> None:
    home = _spool(tmp_path, dead={"01_a.json": "content.captured"}, losses=2)
    sink = InMemoryEmissionSink()
    result = _handler(home, sink).handle(ModelHookSpoolReconcileCommand(replay=False))
    assert result.replayed == 0
    assert _journal_files(home) == []
    assert _alarm_on_journal(sink.events)


# -- failing control ----------------------------------------------------------------------------------------------------


class _DroppingSink(InMemoryEmissionSink):
    def emit(self, event: ModelNodeEmission) -> None:
        return None


def test_control_a_dead_letter_with_no_alarm_fails(tmp_path: Path) -> None:
    """The injected break: the alarm never reaches the journal. The board's check then reads the loss as unalarmed."""
    home = _spool(
        tmp_path,
        dead={"01_a.json": "content.captured"},
        refused=["content.captured"],
        losses=3,
    )
    broken = _DroppingSink()
    _handler(home, broken).handle(ModelHookSpoolReconcileCommand(replay=True))
    assert not _alarm_on_journal(broken.events)
    # and the same spool with the alarm on the journal passes the same check
    home2 = tmp_path / "second"
    home2.mkdir()
    _spool(
        home2,
        dead={"01_a.json": "content.captured"},
        refused=["content.captured"],
        losses=3,
    )
    good = InMemoryEmissionSink()
    _handler(home2, good).handle(ModelHookSpoolReconcileCommand(replay=True))
    assert _alarm_on_journal(good.events)


# -- parity and seam ----------------------------------------------------------------------------------------------------


def test_parity_in_memory_and_jsonl_sinks_hold_the_same_chain(tmp_path: Path) -> None:
    homes = (tmp_path / "m", tmp_path / "j")
    for home in homes:
        home.mkdir()
        _spool(
            home,
            dead={"01_a.json": "content.captured"},
            refused=["content.captured"],
            losses=1,
        )
    mem = InMemoryEmissionSink()
    disk = JsonlEmissionSink(tmp_path / "emissions.jsonl")
    _handler(homes[0], mem).handle(ModelHookSpoolReconcileCommand(replay=True))
    _handler(homes[1], disk).handle(ModelHookSpoolReconcileCommand(replay=True))
    read_back = disk.read(datetime(2000, 1, 1, tzinfo=UTC))
    assert [(e.phase, e.failure_code) for e in mem.events] == [
        (e.phase, e.failure_code) for e in read_back
    ]


def test_seam_the_files_the_drainer_writes_are_read_as_the_node_reads_them(
    tmp_path: Path,
) -> None:
    """Pins both sides: the drainer's real quarantine record, reason, status and loss line (copied from h053)."""
    q = tmp_path / "hook_emit_journal/quarantine"
    q.mkdir(parents=True)
    name = "01794496486798429630_8e6fa593-c01d-4edb-81ff-4b3646b82806.json"
    shutil.copy(FIXTURES / "quarantine_record.json", q / name)
    shutil.copy(
        FIXTURES / "quarantine_reason.json", q / name.replace(".json", ".reason.json")
    )
    shutil.copy(
        FIXTURES / "drainer_status.json", tmp_path / "hook_emit_drainer_status.json"
    )
    shutil.copy(
        FIXTURES / "loss_line.jsonl", tmp_path / "hook_emit_journal_losses.jsonl"
    )
    # the keys the node reads must be keys of the real files
    record = json.loads((FIXTURES / "quarantine_record.json").read_text())
    assert {"event_type", "queued_at"} <= set(record)
    assert "refused_event_types" in json.loads(
        (FIXTURES / "drainer_status.json").read_text()
    )
    sink = InMemoryEmissionSink()
    result = _handler(tmp_path, sink).handle(
        ModelHookSpoolReconcileCommand(replay=False)
    )
    assert result.before.by_class == {"content.captured": 1}
    assert result.before.losses_total == 1
    assert result.before.refused_event_types == ()
    assert result.before.dead_lettered == 1
    assert result.before.unreadable == 0
    assert result.alarmed_losses == 1


def test_contract_terminal_event_is_the_declared_completed_topic() -> None:
    contract = yaml.safe_load(
        OVERLAY_EXAMPLE.with_name("contract.yaml").read_text(encoding="utf-8")
    )
    completed = "onex.evt.omnimarket.hook-spool-reconcile-completed.v1"
    assert contract["terminal_event"] == completed
    assert contract["topics"]["hook_spool_reconcile_completed"] == completed
    assert completed in contract["externally_consumed_topics"]
