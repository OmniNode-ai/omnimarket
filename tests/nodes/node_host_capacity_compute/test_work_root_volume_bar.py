# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A work root on a volume above the bar is refused, one at or below it is admitted (OMN-26371).

The bar is the ``host_capacity`` overlay's ``max_work_root_volume_used_percent``; the volume reading is the live one
(``shutil.disk_usage`` through ``read_work_root_volume_used_percent``), never a number held in code.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from omnimarket.handlers.host_capacity_overlay import load_host_capacity_limits
from omnimarket.nodes.node_host_capacity_compute import HandlerHostCapacity
from omnimarket.nodes.node_host_capacity_compute.handlers.handler_host_capacity import (
    read_work_root_volume_used_percent,
)
from omnimarket.nodes.node_host_capacity_compute.models import (
    ModelHostCapacityLimits,
    ModelHostCapacityReading,
    ModelHostCapacityRequest,
    ModelHostCapacityVerdict,
)
from omnimarket.nodes.node_lab_work_unit_effect.handlers.handler_host_capacity_advertise_effect import (
    HandlerHostCapacityAdvertiseEffect,
)
from omnimarket.nodes.node_lab_work_unit_effect.models.model_lab_work_unit import (
    ModelHostCapacityProbeRequest,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols.local_host_reader import (
    HostReading,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("host_capacity_overlay")]

ROOT = "/mnt/example/lab-run"


def judge(
    percent: float | None,
    *,
    root: str | None = ROOT,
    lim: ModelHostCapacityLimits | None = None,
) -> ModelHostCapacityVerdict:
    return HandlerHostCapacity().handle(
        ModelHostCapacityRequest(
            reading=ModelHostCapacityReading(
                host="h101",
                logical_cores=12,
                mem_total_gb=32.0,
                mem_avail_gb=24.0,
                load_one_minute=0.5,
                running_lanes=0,
                swap_in_kb_s=0.0,
                swap_out_kb_s=0.0,
                work_root=root,
                work_root_volume_used_percent=percent,
            ),
            limits=lim or load_host_capacity_limits(),
        )
    )


def test_work_root_volume_bar_is_the_overlay_85() -> None:
    assert load_host_capacity_limits().max_work_root_volume_used_percent == 85.0


def test_work_root_volume_bar_refuses_above_it_naming_host_root_and_percent() -> None:
    v = judge(91.0)
    assert not v.admitted
    assert v.free_slots == 0
    (reason,) = v.refusals
    for part in ("h101", ROOT, "91.0", "85"):
        assert part in reason


@pytest.mark.parametrize("percent", [85.0, 84.9, 37.0, 0.0])
def test_work_root_volume_bar_admits_at_or_below_it(percent: float) -> None:
    v = judge(percent)
    assert v.admitted, v.refusals
    assert v.refusals == ()


def test_work_root_volume_bar_just_over_is_refused() -> None:
    assert not judge(85.1).admitted


def test_work_root_volume_bar_removed_admits_the_full_volume() -> None:
    """Control: with no bar declared the same reading is admitted, so the refusal above comes from the bar."""
    lim = load_host_capacity_limits().model_copy(
        update={"max_work_root_volume_used_percent": None}
    )
    assert judge(99.0, lim=lim).admitted


def test_work_root_volume_bar_unread_volume_is_not_judged() -> None:
    assert judge(None).admitted
    assert judge(99.0, root=None).admitted


def test_work_root_volume_bar_reads_the_live_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage = shutil.disk_usage(tmp_path)
    live = read_work_root_volume_used_percent(str(tmp_path))
    assert live == pytest.approx(
        100.0 * usage.used / (usage.used + usage.free), abs=1.0
    )
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_capacity_compute.handlers.handler_host_capacity.shutil.disk_usage",
        lambda _path: shutil._ntuple_diskusage(1000, 910, 90),
    )
    assert read_work_root_volume_used_percent(str(tmp_path)) == pytest.approx(91.0)


class _Reader:
    def read(self, tools: list[str]) -> HostReading:
        return HostReading(
            cores=12,
            load1=0.5,
            mem_available_bytes=24 * 1024**3,
            mem_total_bytes=32 * 1024**3,
            tools=("git",),
            swap_in_kb_s=0.0,
            swap_out_kb_s=0.0,
        )


def _advertise(work_root: str | None) -> tuple[int, str]:
    ad = HandlerHostCapacityAdvertiseEffect(
        reader=_Reader(), watermark=lambda _host: None
    ).handle(ModelHostCapacityProbeRequest(host_name="h101", work_root=work_root))
    return ad.max_units, ad.capacity_refusal


def test_work_root_volume_bar_reaches_the_advertisement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The serve's advertisement reads the live volume of its work root and refuses a host above the bar."""
    root = str(tmp_path / "lab-run")
    target = "omnimarket.nodes.node_host_capacity_compute.handlers.handler_host_capacity.shutil.disk_usage"
    monkeypatch.setattr(target, lambda _p: shutil._ntuple_diskusage(1000, 910, 90))
    _, refusal = _advertise(root)
    for part in (root, "91.0", "h101"):
        assert part in refusal
    monkeypatch.setattr(target, lambda _p: shutil._ntuple_diskusage(1000, 850, 150))
    assert _advertise(root)[1] == ""
    assert _advertise(None)[1] == ""
