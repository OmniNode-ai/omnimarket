# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lab disk watermark (OMN-23384): reading file, levels, page exit and the timer units."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.handler_lab_disk_watermark import (
    HandlerLabDiskWatermark,
    Usage,
    WatermarkRefusalError,
    load_schedule,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.models.model_lab_disk_watermark import (
    ModelLabDiskWatermarkRequest,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.node import (
    NodeLabDiskWatermarkEffect,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.runtime_lab_disk_watermark import (
    main,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.watermark_schedule import (
    WatermarkScheduler,
    render_units,
)

FIXED = datetime(2026, 11, 9, 21, 0, 0, tzinfo=UTC)
GIB = 1024**3


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
                        "volumes": [
                            {"path": "/", "warn_percent": 80, "page_percent": 90}
                        ],
                    }
                ]
            }
        )
    )
    return path


def _handler(tmp_path: Path, used_percent: int) -> HandlerLabDiskWatermark:
    return HandlerLabDiskWatermark(
        usage=lambda _path: Usage(
            100 * GIB, used_percent * GIB, (100 - used_percent) * GIB
        ),
        now=lambda: FIXED,
        home=tmp_path,
    )


def _request(tmp_path: Path) -> ModelLabDiskWatermarkRequest:
    return ModelLabDiskWatermarkRequest(
        host_name="pool-a", overlay_file=str(_overlay(tmp_path))
    )


@pytest.mark.parametrize(
    ("used", "level"),
    [(50, "ok"), (80, "warn"), (89, "warn"), (90, "page"), (96, "page")],
)
def test_levels_follow_the_declared_marks(
    tmp_path: Path, used: int, level: str
) -> None:
    reading = _handler(tmp_path, used).handle(_request(tmp_path))
    assert reading.level == level
    assert reading.paged is (level == "page")


def test_golden_chain_node_entry_writes_the_reading_file(tmp_path: Path) -> None:
    node = NodeLabDiskWatermarkEffect()
    node.handler = _handler(tmp_path, 96)
    reading = node.handle(_request(tmp_path))
    written = json.loads((tmp_path / "state" / "pool-a.json").read_text())
    assert written["level"] == "page"
    assert written["read_at"] == "2026-11-09T21:00:00Z"
    assert written["volumes"][0]["used_percent"] == 96.0
    assert reading.reading_file == str(tmp_path / "state" / "pool-a.json")


def test_dry_run_writes_no_file(tmp_path: Path) -> None:
    reading = _handler(tmp_path, 50).handle(
        _request(tmp_path).model_copy(update={"dry_run": True})
    )
    assert reading.written is False
    assert not (tmp_path / "state").exists()


def test_error_chain_a_host_without_a_row_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(WatermarkRefusalError, match="no watermark is declared"):
        _handler(tmp_path, 50).handle(
            _request(tmp_path).model_copy(update={"host_name": "pool-z"})
        )
    code = main(
        ["run", "--host", "pool-z", "--overlay", str(tmp_path / "overlay.yaml")]
    )
    assert code == 3
    assert "INDETERMINATE" in capsys.readouterr().err


def test_error_chain_a_page_exits_two_with_a_page_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _overlay(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "omnimarket.nodes.node_lab_disk_watermark_effect.handlers.handler_lab_disk_watermark.disk_usage",
        lambda _path: Usage(100 * GIB, 96 * GIB, 4 * GIB),
    )
    code = main(
        ["run", "--host", "pool-a", "--overlay", str(tmp_path / "overlay.yaml")]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert captured.err.startswith("PAGE pool-a: / 96.0% used")
    assert json.loads(captured.out)["paged"] is True


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        out = "onex-disk-watermark.timer\n" if "list-timers" in command else ""
        return subprocess.CompletedProcess(list(command), 0, out, "")


def test_timer_units_run_the_node_with_the_overlay_pin(tmp_path: Path) -> None:
    service, timer = render_units(
        load_schedule(),
        "pool-a",
        python=Path("/usr/bin/python3"),
        source_root=Path("/srv/omnimarket"),
        overlay_file=Path("/srv/overlay.yaml"),
        home=tmp_path,
    )
    assert (
        '-m omnimarket.nodes.node_lab_disk_watermark_effect run --host "pool-a"'
        in service
    )
    assert 'Environment="ONEX_LAB_DISK_WATERMARK_OVERLAY=/srv/overlay.yaml"' in service
    assert "OnUnitActiveSec=300s" in timer
    assert "Unit=onex-disk-watermark.service" in timer


def test_install_writes_units_and_enables_the_timer(tmp_path: Path) -> None:
    overlay = _overlay(tmp_path)
    recorder = _Recorder()
    scheduler = WatermarkScheduler(
        schedule=load_schedule(), home=tmp_path, run=recorder
    )
    python = tmp_path / "python"
    python.write_text("")
    result = scheduler.install(
        "pool-a", python=python, source_root=tmp_path, overlay_file=overlay
    )
    assert result.exit_code == 0, result.stderr
    assert (tmp_path / ".config/systemd/user/onex-disk-watermark.timer").is_file()
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "onex-disk-watermark.timer",
    ] in recorder.calls
