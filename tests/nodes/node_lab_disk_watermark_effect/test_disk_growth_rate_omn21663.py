# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The disk alarm adds a growth rate to its fixed floor (OMN-21663).

The recorded case: the disk alarm was a fixed 10 GB floor with no growth-rate check, so a disk that fell by many
GB an hour paged only after it was already at the floor. These tests take two real readings of one host, the
second later with less free space, and read what the second one says about the time left to the floor.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.handler_lab_disk_watermark import (
    HandlerLabDiskWatermark,
    Usage,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.models.model_lab_disk_watermark import (
    ModelLabDiskWatermarkReading,
    ModelLabDiskWatermarkRequest,
    ModelWatermarkHost,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.runtime_lab_disk_watermark import (
    main,
)

FIRST = datetime(2026, 11, 9, 20, 0, 0, tzinfo=UTC)
GIB = 1024**3


def _overlay(tmp_path: Path, growth: dict[str, object] | None) -> Path:
    host: dict[str, object] = {
        "name": "pool-a",
        "scheduler": "systemd-user",
        "reading_file": "$HOME/state/pool-a.json",
        "admission_floor_gb": 5,
        "volumes": [{"path": "/", "warn_percent": 80, "page_percent": 90}],
    }
    if growth is not None:
        host["growth"] = growth
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump({"hosts": [host]}))
    return path


def _read(
    tmp_path: Path, at: datetime, free_gb: int, growth: dict[str, object] | None
) -> object:
    handler = HandlerLabDiskWatermark(
        usage=lambda _path: Usage(100 * GIB, (100 - free_gb) * GIB, free_gb * GIB),
        now=lambda: at,
        home=tmp_path,
    )
    return handler.handle(
        ModelLabDiskWatermarkRequest(
            host_name="pool-a", overlay_file=str(_overlay(tmp_path, growth))
        )
    )


GROWTH = {"page_hours_to_floor": 12, "min_span_minutes": 30}


def test_disk_growth_rate_pages_when_the_floor_is_near_at_the_measured_rate(
    tmp_path: Path,
) -> None:
    _read(tmp_path, FIRST, 50, GROWTH)
    reading = _read(tmp_path, FIRST + timedelta(hours=2), 40, GROWTH)
    assert reading.level == "ok"
    assert reading.growth_paged is True
    volume = reading.growth[0]
    assert volume.free_gb_per_hour == pytest.approx(5.0)
    assert volume.hours_to_floor == pytest.approx(7.0)
    saved = json.loads((tmp_path / "state/pool-a.json").read_text())
    assert saved["level"] == "ok"
    assert "growth" not in saved
    assert "growth_paged" not in saved


def test_disk_growth_rate_does_not_page_when_the_floor_is_far(tmp_path: Path) -> None:
    _read(tmp_path, FIRST, 50, GROWTH)
    reading = _read(
        tmp_path, FIRST + timedelta(hours=2), 40, {**GROWTH, "page_hours_to_floor": 6}
    )
    assert reading.growth_paged is False
    assert reading.growth[0].hours_to_floor == pytest.approx(7.0)


def test_disk_growth_rate_a_shorter_span_than_declared_makes_no_rate(
    tmp_path: Path,
) -> None:
    _read(tmp_path, FIRST, 50, GROWTH)
    reading = _read(tmp_path, FIRST + timedelta(minutes=10), 10, GROWTH)
    assert reading.growth_paged is False
    assert reading.growth == ()


def test_disk_growth_rate_growing_free_space_is_never_a_page(tmp_path: Path) -> None:
    _read(tmp_path, FIRST, 20, GROWTH)
    reading = _read(tmp_path, FIRST + timedelta(hours=2), 40, GROWTH)
    assert reading.growth_paged is False
    assert reading.growth[0].hours_to_floor is None


def test_disk_growth_rate_first_reading_has_nothing_to_compare(tmp_path: Path) -> None:
    reading = _read(tmp_path, FIRST, 10, GROWTH)
    assert reading.growth == ()
    assert reading.growth_paged is False


def test_disk_growth_rate_is_off_when_the_overlay_declares_none(tmp_path: Path) -> None:
    _read(tmp_path, FIRST, 50, None)
    reading = _read(tmp_path, FIRST + timedelta(hours=2), 20, None)
    assert reading.growth == ()
    assert reading.growth_paged is False


def test_disk_growth_rate_overlay_refuses_a_zero_horizon() -> None:
    with pytest.raises(ValueError, match="page_hours_to_floor"):
        ModelWatermarkHost.model_validate(
            {
                "name": "pool-a",
                "scheduler": "systemd-user",
                "reading_file": "$HOME/x.json",
                "admission_floor_gb": 5,
                "volumes": [{"path": "/", "warn_percent": 80, "page_percent": 90}],
                "growth": {"page_hours_to_floor": 0, "min_span_minutes": 30},
            }
        )


def test_disk_growth_rate_run_exits_at_the_page_code_and_names_the_rate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.nodes.node_lab_disk_watermark_effect import (
        runtime_lab_disk_watermark,
    )

    overlay = _overlay(tmp_path, GROWTH)
    _read(tmp_path, FIRST, 50, GROWTH)
    handler = HandlerLabDiskWatermark(
        usage=lambda _path: Usage(100 * GIB, 60 * GIB, 40 * GIB),
        now=lambda: FIRST + timedelta(hours=2),
        home=tmp_path,
    )
    monkeypatch.setattr(
        runtime_lab_disk_watermark, "HandlerLabDiskWatermark", lambda: handler
    )
    code = main(["run", "--host", "pool-a", "--overlay", str(overlay)])
    err = capsys.readouterr().err
    assert code == 2
    assert "PAGE-GROWTH pool-a" in err
    assert "5.0 GB an hour" in err
    assert "7.0 hours" in err


def test_disk_growth_rate_the_written_file_validates_as_the_contract_model(
    tmp_path: Path,
) -> None:
    """The reading file is the contract's own model: a reader validating it with extra=forbid must not fail."""
    _read(tmp_path, FIRST, 50, GROWTH)
    reading = _read(tmp_path, FIRST + timedelta(hours=2), 40, GROWTH)
    assert reading.growth_paged is True
    saved = json.loads((tmp_path / "state/pool-a.json").read_text())
    parsed = ModelLabDiskWatermarkReading.model_validate(saved)
    assert parsed.level == "ok"
    assert parsed.read_at == (FIRST + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_disk_growth_rate_short_ticks_over_a_long_fall_still_make_a_rate(
    tmp_path: Path,
) -> None:
    """Ticks every 5 minutes against a 30 minute minimum span: the previous tick is always too young, so the
    baseline must be an older reading, and a steady fall of 6 GB an hour must page once 30 minutes have passed."""
    readings = []
    for tick in range(0, 13):
        free_gb = 60 - tick / 2
        readings.append(
            _read(tmp_path, FIRST + timedelta(minutes=5 * tick), int(free_gb), GROWTH)
        )
    assert readings[0].growth == ()
    assert readings[5].growth == ()
    assert readings[6].growth
    assert readings[12].growth
    last = readings[12]
    assert last.growth[0].free_gb_per_hour == pytest.approx(6.0, abs=0.7)
    assert last.growth[0].span_minutes >= 30
    assert last.growth_paged is True


def test_disk_growth_rate_a_dry_run_keeps_no_history(tmp_path: Path) -> None:
    handler = HandlerLabDiskWatermark(
        usage=lambda _path: Usage(100 * GIB, 50 * GIB, 50 * GIB),
        now=lambda: FIRST,
        home=tmp_path,
    )
    handler.handle(
        ModelLabDiskWatermarkRequest(
            host_name="pool-a",
            overlay_file=str(_overlay(tmp_path, GROWTH)),
            dry_run=True,
        )
    )
    assert not (tmp_path / "state").exists()
