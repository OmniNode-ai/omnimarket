# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The volume read carries the filesystem's file nodes (OMN-24613): so inode pressure can be seen.

The host hygiene schedule can only trigger on inodes when the watermark node's read event reports
them. A filesystem that reports no file-node count (f_files 0) reads none, never a guess.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

import pytest

from omnimarket.models.model_host_disk_reading import ModelHostDiskRead
from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.handler_lab_disk_watermark import (
    disk_usage,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.host_disk_ports import (
    InMemoryHostDiskPublisher,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.models.model_lab_disk_watermark import (
    ModelLabDiskWatermarkRequest,
)

from .host_disk_inputs import handler, make_host, overlay_document, write_overlay

pytestmark = pytest.mark.unit


class _StatvfsNoFileNodes(NamedTuple):
    """The os.statvfs shape with f_files 0: no file-node count to read."""

    f_bsize: int
    f_frsize: int
    f_blocks: int
    f_bfree: int
    f_bavail: int
    f_files: int
    f_ffree: int
    f_favail: int
    f_flag: int
    f_namemax: int


def test_disk_usage_carries_the_file_node_counts(tmp_path: Path) -> None:
    """A real statvfs read fills both inode counts on the usage.

    Failing control: a Usage built without them reads a volume without them
    (``test_a_usage_without_file_nodes_reads_a_volume_without_them``).
    """
    usage = disk_usage(str(tmp_path))
    assert usage.total_inodes is not None
    assert usage.total_inodes >= 1
    assert usage.used_inodes is not None
    assert 0 <= usage.used_inodes <= usage.total_inodes


def test_a_usage_without_file_nodes_reads_a_volume_without_them(
    tmp_path: Path,
) -> None:
    """A three-field Usage, as every injected test usage is built, publishes a volume with None inodes.

    Failing control: the same run over a real ``disk_usage`` read carries the
    counts (``test_disk_usage_carries_the_file_node_counts``).
    """
    paths = make_host(tmp_path)
    overlay = write_overlay(tmp_path, overlay_document(paths))
    publisher = InMemoryHostDiskPublisher()
    reading = handler(tmp_path, publisher).handle(
        ModelLabDiskWatermarkRequest(host_name="pool-a", overlay_file=str(overlay))
    )
    read = reading.host_disk
    assert isinstance(read, ModelHostDiskRead)
    assert read.volumes
    for volume in read.volumes:
        assert volume.total_inodes is None
        assert volume.used_inodes is None


def test_a_filesystem_without_a_file_node_count_reads_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """f_files 0 reads None for both inode counts, the byte counts reading as usual.

    Failing control: the same statvfs shape with f_files set reads both counts
    (``test_disk_usage_carries_the_file_node_counts``).
    """
    monkeypatch.setattr(
        os,
        "statvfs",
        lambda _path: _StatvfsNoFileNodes(
            f_bsize=4096,
            f_frsize=4096,
            f_blocks=100,
            f_bfree=50,
            f_bavail=50,
            f_files=0,
            f_ffree=0,
            f_favail=0,
            f_flag=0,
            f_namemax=255,
        ),
    )
    usage = disk_usage(str(tmp_path))
    assert usage.total == 100 * 4096
    assert usage.used == 50 * 4096
    assert usage.total_inodes is None
    assert usage.used_inodes is None
