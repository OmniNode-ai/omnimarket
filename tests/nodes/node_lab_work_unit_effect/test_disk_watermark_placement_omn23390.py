# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host past its disk page mark advertises it and pool placement refuses new units there (OMN-23390).

The page mark sits above the admission floor, so placement stops before the floor refuses. The golden test places a host
whose reading file reads ok, the error tests refuse a paged host and refuse an overlay whose page mark is not above the floor.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.overlay_keys import ModelLabHostRow, ModelLabHostsTable
from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.handler_lab_disk_watermark import (
    HandlerLabDiskWatermark,
    Usage,
    WatermarkRefusalError,
    read_level,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.models.model_lab_disk_watermark import (
    ModelLabDiskWatermarkRequest,
)
from omnimarket.nodes.node_lab_placement_compute.handlers import HandlerLabPoolPlace
from omnimarket.nodes.node_lab_placement_compute.models import (
    EnumPlacementDecision,
    ModelLabPoolPlaceRequest,
    ModelPlacement,
)
from omnimarket.nodes.node_lab_work_unit_effect.handlers.handler_host_capacity_advertise_effect import (
    HandlerHostCapacityAdvertiseEffect,
)
from omnimarket.nodes.node_lab_work_unit_effect.models.model_lab_work_unit import (
    ModelHostCapacityProbeRequest,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols.local_host_reader import (
    DISK_WATERMARK_PAGE_TOOL,
    HostReading,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 9, 23, 0, 0, tzinfo=UTC)
GIB = 1024**3


class _Reader:
    def read(self, tools: list[str]) -> HostReading:
        return HostReading(
            cores=8, load1=0.5, mem_available_bytes=40 * GIB, tools=("git",)
        )


def _overlay(tmp_path: Path, floor: float = 10) -> Path:
    path = tmp_path / "overlay.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "hosts": [
                    {
                        "name": "pool-a",
                        "scheduler": "systemd-user",
                        "reading_file": f"{tmp_path}/pool-a.json",
                        "admission_floor_gb": floor,
                        "volumes": [
                            {"path": "/", "warn_percent": 70, "page_percent": 80}
                        ],
                    }
                ]
            }
        )
    )
    return path


def _reading(tmp_path: Path, used_gib: int) -> None:
    HandlerLabDiskWatermark(
        usage=lambda _p: Usage(100 * GIB, used_gib * GIB, (100 - used_gib) * GIB),
        now=lambda: NOW,
        home=tmp_path,
    ).handle(
        ModelLabDiskWatermarkRequest(
            host_name="pool-a", overlay_file=str(_overlay(tmp_path))
        )
    )


def _advertise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> ModelHostCapacityAdvertisement:
    monkeypatch.setenv("ONEX_LAB_DISK_WATERMARK_OVERLAY", str(_overlay(tmp_path)))
    handler = HandlerHostCapacityAdvertiseEffect(reader=_Reader(), now=lambda: NOW)
    return handler.handle(
        ModelHostCapacityProbeRequest(
            host_name="pool-a",
            tools=[],
            running_units=0,
            max_units=2,
            cadence_seconds=10,
        )
    )


def _place(ad: ModelHostCapacityAdvertisement) -> ModelPlacement:
    table = ModelLabHostsTable(hosts=(ModelLabHostRow(name="pool-a", target="pool-a"),))
    return HandlerLabPoolPlace(environ={}).handle(
        ModelLabPoolPlaceRequest(
            advertisements=(
                ad.model_copy(update={"advertised_at": NOW - timedelta(seconds=1)}),
            ),
            evaluated_at=NOW,
            table=table,
        )
    )


def test_golden_chain_a_host_below_the_page_mark_advertises_no_token_and_is_placed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reading(tmp_path, 50)
    ad = _advertise(tmp_path, monkeypatch)
    assert DISK_WATERMARK_PAGE_TOOL not in ad.tools
    assert _place(ad).decision is EnumPlacementDecision.PLACED


def test_error_chain_a_host_past_the_page_mark_advertises_it_and_takes_no_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reading(tmp_path, 85)
    ad = _advertise(tmp_path, monkeypatch)
    assert DISK_WATERMARK_PAGE_TOOL in ad.tools
    placement = _place(ad)
    assert placement.decision is EnumPlacementDecision.REFUSED
    assert "disk watermark" in placement.verdicts[0].reason


def test_a_host_with_no_overlay_or_no_reading_yet_advertises_no_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_LAB_DISK_WATERMARK_OVERLAY", raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    assert read_level("pool-a", home=tmp_path) is None
    assert DISK_WATERMARK_PAGE_TOOL not in _advertise(tmp_path, monkeypatch).tools


def test_error_chain_a_corrupt_reading_file_raises_and_does_not_read_as_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_LAB_DISK_WATERMARK_OVERLAY", str(_overlay(tmp_path)))
    (tmp_path / "pool-a.json").write_text(json.dumps({"level": "fine"}))
    with pytest.raises(WatermarkRefusalError, match="holds no level"):
        read_level("pool-a", home=tmp_path)


def test_error_chain_a_page_mark_not_above_the_admission_floor_is_refused(
    tmp_path: Path,
) -> None:
    """80% page on 100 GiB leaves 20 GiB free: a 25 GiB floor would refuse placement before the page fires."""
    with pytest.raises(
        WatermarkRefusalError, match="not above the 25 GB admission floor"
    ):
        HandlerLabDiskWatermark(
            usage=lambda _p: Usage(100 * GIB, 50 * GIB, 50 * GIB),
            now=lambda: NOW,
            home=tmp_path,
        ).handle(
            ModelLabDiskWatermarkRequest(
                host_name="pool-a", overlay_file=str(_overlay(tmp_path, floor=25))
            )
        )
