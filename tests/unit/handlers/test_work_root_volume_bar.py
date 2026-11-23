# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lane work root on a volume above 85 percent used is refused; one at or below the bar is admitted (OMN-26371)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omnimarket.handlers.work_root_volume_bar import (
    VOLUME_BAR_PERCENT,
    ModelVolumeReading,
    VolumeReader,
    WorkRootVolumeError,
    read_live_volume,
    refuse_work_root_over_bar,
)

GIB = 1024**3


def _volume(used_percent_tenths: int) -> VolumeReader:
    """A reader whose volume is ``used_percent_tenths`` / 10 percent used of 1000 GiB."""

    def reader(path: Path) -> ModelVolumeReading:
        used = used_percent_tenths * GIB
        return ModelVolumeReading(
            path=str(path), used_bytes=used, available_bytes=1000 * GIB - used
        )

    return reader


def test_work_root_volume_bar_is_85_percent() -> None:
    assert VOLUME_BAR_PERCENT == 85.0


@pytest.mark.parametrize("tenths", [0, 500, 849, 850])
def test_work_root_volume_bar_admits_a_volume_at_or_below_the_bar(tenths: int) -> None:
    reading = refuse_work_root_over_bar(Path("/data/lab-run"), reader=_volume(tenths))
    assert reading.used_percent == pytest.approx(tenths / 10)


@pytest.mark.parametrize("tenths", [851, 880, 910, 1000])
def test_work_root_volume_bar_refuses_a_volume_above_the_bar(tenths: int) -> None:
    with pytest.raises(WorkRootVolumeError, match="above the 85 percent bar"):
        refuse_work_root_over_bar(Path("/srv/boot/lab-run"), reader=_volume(tenths))


def test_work_root_volume_bar_refuses_a_volume_that_cannot_be_read() -> None:
    def broken(path: Path) -> ModelVolumeReading:
        raise OSError("input/output error")

    with pytest.raises(WorkRootVolumeError, match="could not be read"):
        refuse_work_root_over_bar(Path("/srv/data/lab-run"), reader=broken)


def test_work_root_volume_bar_reads_the_live_volume(tmp_path: Path) -> None:
    """The default reader is statvfs of the root's volume, through the nearest existing parent for a root not created yet."""
    root = tmp_path / "not" / "yet" / "lab-run"
    reading = read_live_volume(root)
    stat = os.statvfs(tmp_path)
    # Other processes write to a live volume between the two reads, so the figures agree within a percent, not exactly.
    assert reading.available_bytes == pytest.approx(
        stat.f_bavail * stat.f_frsize, rel=0.01
    )
    assert reading.used_bytes == pytest.approx(
        (stat.f_blocks - stat.f_bfree) * stat.f_frsize, rel=0.01
    )
    # The bar judges that live figure: a bar under the live reading refuses, one above it admits.
    live = reading.used_percent
    with pytest.raises(WorkRootVolumeError):
        refuse_work_root_over_bar(root, bar_percent=live - 2.0)
    assert refuse_work_root_over_bar(root, bar_percent=live + 2.0).path == str(root)
