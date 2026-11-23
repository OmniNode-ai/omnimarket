# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Inode alarm of the lab disk watermark (OMN-24562): below 15 percent free inodes, the reaper's rows, naming the owner.

The falsifier pair: 14 percent free alarms with an ``owner=`` cell in both rows, 16 percent free does not.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers import ledger_row_grammar as grammar
from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.handler_lab_disk_watermark import (
    HandlerLabDiskWatermark,
    Usage,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.inode_alarm import (
    InodeUsage,
    load_inode_alarm,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.models.model_lab_disk_watermark import (
    ModelLabDiskWatermarkRequest,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.runtime_lab_disk_watermark import (
    main,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.watermark_schedule import (
    render_units,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelAppendOutcome

FIXED = datetime(2026, 11, 23, 17, 0, 0, tzinfo=UTC)
GIB = 1024**3
TOTAL = 1_000_000


class _Appender:
    """Records what the alarm appends; ``status`` injects a refused or failed append."""

    def __init__(self, status: str = "accepted") -> None:
        self.status = status
        self.calls: list[str] = []

    def append(self, rows: str, request_id: str) -> ModelAppendOutcome:
        self.calls.append(rows)
        return ModelAppendOutcome(status=self.status, message=request_id)


def _overlay(tmp_path: Path) -> Path:
    path = tmp_path / "overlay.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "hosts": [
                    {
                        "name": "pool-a",
                        "scheduler": "systemd-user",
                        "reading_file": "$HOME/state/pool-a.json",
                        "admission_floor_gb": 5,
                        "volumes": [
                            {"path": "/", "warn_percent": 80, "page_percent": 90}
                        ],
                    }
                ]
            }
        )
    )
    return path


def _handler(
    tmp_path: Path,
    free_percent: float,
    appender: _Appender | None,
    now: datetime = FIXED,
) -> HandlerLabDiskWatermark:
    return HandlerLabDiskWatermark(
        usage=lambda _path: Usage(100 * GIB, 50 * GIB, 50 * GIB),
        now=lambda: now,
        home=tmp_path,
        inodes=lambda _path: InodeUsage(TOTAL, int(TOTAL * free_percent / 100)),
        inode_appender=appender,
    )


def _request(tmp_path: Path, dry_run: bool = False) -> ModelLabDiskWatermarkRequest:
    return ModelLabDiskWatermarkRequest(
        host_name="pool-a", overlay_file=str(_overlay(tmp_path)), dry_run=dry_run
    )


def test_the_contract_declares_the_threshold_and_a_named_owner() -> None:
    declaration = load_inode_alarm()
    assert declaration.free_percent_below == 15
    assert declaration.owner.strip()
    assert declaration.ledger_append_command_env == "ONEX_LEDGER_BUS_APPEND_COMMAND"


def test_14_percent_free_alarms_with_an_owner_in_both_rows(tmp_path: Path) -> None:
    appender = _Appender()
    reading = _handler(tmp_path, 14, appender).handle(_request(tmp_path))
    assert len(reading.inode_alarms) == 1
    alarm = reading.inode_alarms[0]
    assert alarm.written is True
    owner = load_inode_alarm().owner
    friction, status = alarm.rows
    assert " | FRICTION | " in friction
    assert f"owner={owner}" in friction
    assert " | STATUS | " in status
    assert f"owner={owner}" in status
    assert status.endswith("alarms=1")
    assert appender.calls == ["\n".join(alarm.rows) + "\n"]


def test_16_percent_free_does_not_alarm(tmp_path: Path) -> None:
    appender = _Appender()
    reading = _handler(tmp_path, 16, appender).handle(_request(tmp_path))
    assert reading.inode_alarms == ()
    assert reading.inodes[0].alarm is False
    assert appender.calls == []


def test_exactly_15_percent_free_is_not_below_the_share(tmp_path: Path) -> None:
    reading = _handler(tmp_path, 15, _Appender()).handle(_request(tmp_path))
    assert reading.inode_alarms == ()


def test_the_rows_parse_under_the_ledger_grammar(tmp_path: Path) -> None:
    reading = _handler(tmp_path, 5, _Appender()).handle(_request(tmp_path))
    for row, kind in zip(
        reading.inode_alarms[0].rows, ("FRICTION", "STATUS"), strict=True
    ):
        parsed = grammar.parse_row(row)
        assert parsed is not None
        assert parsed.row_type == kind
        assert parsed.fields["owner"][0] == load_inode_alarm().owner
        assert parsed.fields["lane"][0] == "lab-disk-watermark"


def test_an_alarm_is_written_once_per_interval_not_once_per_tick(
    tmp_path: Path,
) -> None:
    appender = _Appender()
    _handler(tmp_path, 10, appender).handle(_request(tmp_path))
    soon = _handler(tmp_path, 10, appender, FIXED + timedelta(minutes=5)).handle(
        _request(tmp_path)
    )
    assert len(appender.calls) == 1
    assert soon.inode_alarms[0].written is False
    assert "already alarmed" in soon.inode_alarms[0].note
    later = _handler(tmp_path, 10, appender, FIXED + timedelta(minutes=61)).handle(
        _request(tmp_path)
    )
    assert len(appender.calls) == 2
    assert later.inode_alarms[0].written is True


def test_a_refused_append_is_still_an_alarm_that_names_why(tmp_path: Path) -> None:
    reading = _handler(tmp_path, 10, _Appender("refused")).handle(_request(tmp_path))
    alarm = reading.inode_alarms[0]
    assert alarm.written is False
    assert "refused" in alarm.note


def test_a_dry_run_reads_the_alarm_and_appends_nothing(tmp_path: Path) -> None:
    appender = _Appender()
    reading = _handler(tmp_path, 10, appender).handle(_request(tmp_path, dry_run=True))
    assert len(reading.inode_alarms) == 1
    assert appender.calls == []


def test_an_unreadable_volume_is_named_never_skipped_silently(tmp_path: Path) -> None:
    def broken(_path: str) -> InodeUsage:
        raise OSError("no such volume")

    reading = HandlerLabDiskWatermark(
        usage=lambda _path: Usage(100 * GIB, 50 * GIB, 50 * GIB),
        now=lambda: FIXED,
        home=tmp_path,
        inodes=broken,
        inode_appender=_Appender(),
    ).handle(_request(tmp_path))
    assert reading.inodes_unread == ("/",)
    assert reading.inodes == ()


def test_the_reading_file_keeps_its_shape(tmp_path: Path) -> None:
    reading = _handler(tmp_path, 5, _Appender()).handle(_request(tmp_path))
    written = json.loads((tmp_path / "state/pool-a.json").read_text(encoding="utf-8"))
    for field in ("inodes", "inode_alarms", "inodes_unread"):
        assert field not in written
    assert reading.level == "ok"


def test_the_service_exits_2_and_names_the_owner_below_the_share(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_lab_disk_watermark_effect.handlers import (
        handler_lab_disk_watermark as module,
    )

    monkeypatch.setattr(
        module, "inode_usage", lambda _path: InodeUsage(TOTAL, int(TOTAL * 0.14))
    )
    monkeypatch.setattr(
        module, "disk_usage", lambda _path: Usage(100 * GIB, 50 * GIB, 50 * GIB)
    )
    monkeypatch.delenv("ONEX_LEDGER_BUS_APPEND_COMMAND", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(["run", "--host", "pool-a", "--overlay", str(_overlay(tmp_path))])
    assert code == 2
    assert "PAGE-INODES pool-a" in err.getvalue()
    assert f"owner={load_inode_alarm().owner}" in err.getvalue()
    assert "ONEX_LEDGER_BUS_APPEND_COMMAND is not set" in err.getvalue()


def test_the_unit_carries_the_ledger_route_the_alarm_writes_through(
    tmp_path: Path,
) -> None:
    from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.handler_lab_disk_watermark import (
        load_schedule,
    )

    command = (
        "/py -m omnimarket.work_ledger_bus.cli append --bus-lane dev --omnibase-path /w"
    )
    kwargs = {
        "python": Path("/py"),
        "source_root": Path("/src"),
        "overlay_file": Path("/o.yaml"),
        "home": Path("/home/u"),
        "workspace_root": Path("/w"),
        "workspace_config_root": Path("/wc"),
    }
    with_route, _ = render_units(
        load_schedule(), "pool-a", ledger_append_command=command, **kwargs
    )
    without_route, _ = render_units(load_schedule(), "pool-a", **kwargs)
    assert f'Environment="ONEX_LEDGER_BUS_APPEND_COMMAND={command}"' in with_route
    assert "ONEX_LEDGER_BUS_APPEND_COMMAND" not in without_route
