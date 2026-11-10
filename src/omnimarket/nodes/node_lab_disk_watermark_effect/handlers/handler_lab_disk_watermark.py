# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read each declared volume against its marks, write the reading file, report the worst level."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

import yaml

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_lab_disk_watermark_effect.models.model_lab_disk_watermark import (
    Level,
    ModelLabDiskWatermarkOverlay,
    ModelLabDiskWatermarkReading,
    ModelLabDiskWatermarkRequest,
    ModelLabDiskWatermarkSchedule,
    ModelVolumeReading,
    ModelWatermarkHost,
    ModelWatermarkVolume,
)

NODE_NAME = "node_lab_disk_watermark_effect"
OVERLAY_ENV = "ONEX_LAB_DISK_WATERMARK_OVERLAY"
CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"
LEVEL_ORDER: tuple[Level, ...] = ("ok", "warn", "page")


class WatermarkRefusalError(ValueError):
    """The overlay lacks the host, or is unreadable."""


class Usage(NamedTuple):
    total: int
    used: int
    free: int


def disk_usage(path: str) -> Usage:
    usage = shutil.disk_usage(path)
    return Usage(usage.total, usage.used, usage.free)


def load_overlay(overlay_file: str = "") -> ModelLabDiskWatermarkOverlay:
    if overlay_file:
        try:
            document = yaml.safe_load(Path(overlay_file).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise WatermarkRefusalError(
                f"overlay {overlay_file} is unreadable ({type(exc).__name__})"
            ) from None
        return load_structured_overlay(
            NODE_NAME, ModelLabDiskWatermarkOverlay, explicit=document
        )
    return load_structured_overlay(
        NODE_NAME, ModelLabDiskWatermarkOverlay, file_env=OVERLAY_ENV
    )


def load_schedule() -> ModelLabDiskWatermarkSchedule:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    return ModelLabDiskWatermarkSchedule(**document["schedule"]["cadence"])


def host_row(overlay: ModelLabDiskWatermarkOverlay, name: str) -> ModelWatermarkHost:
    rows = [row for row in overlay.hosts if row.name == name]
    if len(rows) != 1:
        raise WatermarkRefusalError(
            f"{name}: {len(rows)} rows in the {NODE_NAME} overlay, so no watermark is declared for it"
        )
    return rows[0]


def level_of(used_percent: float, volume: ModelWatermarkVolume) -> Level:
    if used_percent >= volume.page_percent:
        return "page"
    if used_percent >= volume.warn_percent:
        return "warn"
    return "ok"


def expand(value: str, home: Path) -> Path:
    if value.startswith("$HOME/"):
        value = str(home / value[len("$HOME/") :])
    if not value.startswith("/") or ".." in Path(value).parts or "$" in value:
        raise WatermarkRefusalError(
            f"reading_file {value!r} must begin with / or $HOME/ and hold no .."
        )
    return Path(os.path.normpath(value))


class HandlerLabDiskWatermark:
    """Take one reading; ``usage``, ``now`` and ``home`` are injected so a test names its own."""

    def __init__(
        self,
        *,
        usage: Callable[[str], Usage] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        home: Path | None = None,
    ) -> None:
        self.usage = usage if usage is not None else (lambda path: disk_usage(path))
        self.now = now
        self.home = home if home is not None else Path.home()

    def handle(
        self, request: ModelLabDiskWatermarkRequest
    ) -> ModelLabDiskWatermarkReading:
        row = host_row(load_overlay(request.overlay_file), request.host_name)
        reading_file = expand(row.reading_file, self.home)
        volumes: list[ModelVolumeReading] = []
        for volume in row.volumes:
            usage = self.usage(volume.path)
            percent = 100.0 * usage.used / usage.total if usage.total else 0.0
            volumes.append(
                ModelVolumeReading(
                    path=volume.path,
                    total_bytes=usage.total,
                    free_bytes=usage.free,
                    used_percent=round(percent, 2),
                    warn_percent=volume.warn_percent,
                    page_percent=volume.page_percent,
                    level=level_of(percent, volume),
                )
            )
        worst = max((v.level for v in volumes), key=LEVEL_ORDER.index)
        reading = ModelLabDiskWatermarkReading(
            host_name=row.name,
            read_at=self.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            level=worst,
            paged=worst == "page",
            volumes=tuple(volumes),
            reading_file=str(reading_file),
            written=not request.dry_run,
        )
        if not request.dry_run:
            self._write(reading_file, reading)
        return reading

    def _write(self, target: Path, reading: ModelLabDiskWatermarkReading) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=target.parent, prefix=".reading-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(reading.model_dump(), indent=2) + "\n")
            os.replace(name, target)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise
