# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC4 (OMN-23865): the failing control. A reading that cannot be sound is a typed failure, never a read event."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel

from omnimarket.models.model_host_disk_reading import (
    ModelHostDiskRead,
    ModelHostDiskReadFailed,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.host_disk_ports import (
    InMemoryHostDiskPublisher,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.models.model_lab_disk_watermark import (
    ModelLabDiskWatermarkReading,
    ModelLabDiskWatermarkRequest,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.runtime_lab_disk_watermark import (
    EXIT_READING_FAILED,
    main,
)

from .host_disk_inputs import (
    FAILED_TOPIC,
    GIB,
    READ_TOPIC,
    handler,
    make_host,
    overlay_document,
    write_overlay,
)

pytestmark = pytest.mark.unit


class _DroppingPublisher:
    """The injected break: accepts every publish and sends nothing."""

    def __init__(self) -> None:
        self.published: list[tuple[str, BaseModel]] = []

    def publish(self, topic: str, event: BaseModel) -> None:
        return None


def _golden(result: ModelLabDiskWatermarkReading, publisher: object) -> None:
    """What the golden chain asserts: the read event is on the bus and is the one the run returned."""
    assert isinstance(result.host_disk, ModelHostDiskRead)
    assert publisher.published == [(READ_TOPIC, result.host_disk)]


def _run(
    tmp_path: Path,
    publisher: Any,
    *,
    used_bytes: int = 50 * GIB,
    tolerance: float = 2,
) -> ModelLabDiskWatermarkReading:
    paths = make_host(tmp_path)
    overlay = write_overlay(
        tmp_path, overlay_document(paths, tolerance_percent=tolerance)
    )
    return handler(tmp_path, publisher, used_bytes=used_bytes).handle(
        ModelLabDiskWatermarkRequest(host_name="pool-a", overlay_file=str(overlay))
    )


def test_golden_passes_with_a_real_publisher(tmp_path: Path) -> None:
    publisher = InMemoryHostDiskPublisher()
    _golden(_run(tmp_path, publisher), publisher)


def test_the_golden_goes_red_when_the_publish_is_dropped(tmp_path: Path) -> None:
    publisher = _DroppingPublisher()
    with pytest.raises(AssertionError):
        _golden(_run(tmp_path, publisher), publisher)


def test_class_sizes_above_the_volumes_used_bytes_are_a_typed_failure(
    tmp_path: Path,
) -> None:
    publisher = InMemoryHostDiskPublisher()
    # the made-up host holds about 20 KiB in its classes; the volume says it uses 1 KiB
    result = _run(tmp_path, publisher, used_bytes=1024)
    assert isinstance(result.host_disk, ModelHostDiskReadFailed)
    assert result.host_disk.reason == "class_sizes_exceed_volume"
    assert "tolerance 2%" in result.host_disk.detail
    assert [topic for topic, _ in publisher.published] == [FAILED_TOPIC]


def test_the_overlay_tolerance_decides_where_the_sum_is_refused(tmp_path: Path) -> None:
    probe = InMemoryHostDiskPublisher()
    event = _run(tmp_path / "probe", probe).host_disk
    assert isinstance(event, ModelHostDiskRead)
    held = sum(
        c.total_bytes
        for c in event.classes
        if c.class_name in ("uv_cache", "worktrees", "lab_run")
    )
    inside = _run(
        tmp_path / "inside",
        InMemoryHostDiskPublisher(),
        used_bytes=int(held / 1.05),
        tolerance=10,
    )
    outside = _run(
        tmp_path / "outside",
        InMemoryHostDiskPublisher(),
        used_bytes=int(held / 1.05),
        tolerance=1,
    )
    assert isinstance(inside.host_disk, ModelHostDiskRead)
    assert isinstance(outside.host_disk, ModelHostDiskReadFailed)


def test_a_refused_publish_is_a_typed_failure_naming_the_topic(tmp_path: Path) -> None:
    result = _run(tmp_path, InMemoryHostDiskPublisher(refuse=True))
    assert isinstance(result.host_disk, ModelHostDiskReadFailed)
    assert result.host_disk.reason == "publish_refused"
    assert READ_TOPIC in result.host_disk.detail
    assert "ConnectionError" in result.host_disk.detail
    assert "failed event was not published either" in result.host_disk.detail
    # the level placement reads is written all the same
    assert (tmp_path / "state" / "pool-a.json").is_file()


def test_an_unreadable_volume_is_a_typed_failure_then_the_error_is_raised(
    tmp_path: Path,
) -> None:
    paths = make_host(tmp_path)
    overlay = write_overlay(tmp_path, overlay_document(paths))
    publisher = InMemoryHostDiskPublisher()
    failing = handler(tmp_path, publisher)

    def unreadable(path: str) -> object:
        raise FileNotFoundError(path)

    cast(Any, failing).usage = unreadable
    with pytest.raises(FileNotFoundError):
        failing.handle(
            ModelLabDiskWatermarkRequest(host_name="pool-a", overlay_file=str(overlay))
        )
    ((topic, event),) = publisher.published
    assert topic == FAILED_TOPIC
    assert isinstance(event, ModelHostDiskReadFailed)
    assert event.reason == "volume_unreadable"


def test_the_command_line_exits_four_on_a_failed_reading(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_lab_disk_watermark_effect import (
        runtime_lab_disk_watermark as runtime,
    )

    paths = make_host(tmp_path)
    overlay = write_overlay(tmp_path, overlay_document(paths))
    broken = handler(tmp_path, InMemoryHostDiskPublisher(refuse=True))
    monkeypatch.setattr(runtime, "HandlerLabDiskWatermark", lambda: broken)
    code = main(["run", "--host", "pool-a", "--overlay", str(overlay)])
    assert code == EXIT_READING_FAILED
    assert "READING-FAILED pool-a: publish_refused" in capsys.readouterr().err
