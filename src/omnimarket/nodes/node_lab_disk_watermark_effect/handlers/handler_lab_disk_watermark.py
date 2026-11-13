# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read each declared volume against its marks, write the reading file, report the worst level."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.handlers.overlay_store.bus_opener import workspace_root
from omnimarket.models.model_host_disk_reading import (
    FILESYSTEM_CLASSES,
    FailureReason,
    ModelHostDiskRead,
    ModelHostDiskReadFailed,
    ModelHostDiskVolume,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.adapter_host_disk_bus import (
    LaneBusHostDiskPublisher,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.host_disk_classes import (
    HostDiskClassReader,
    volume_for,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.host_disk_ports import (
    ProtocolHostDiskPublisher,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.models.model_lab_disk_watermark import (
    Level,
    ModelHostDiskReadingSettings,
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


def check_page_mark_above_floor(
    row: ModelWatermarkHost, volume: ModelWatermarkVolume, usage: Usage
) -> None:
    """Refuse a page mark that is not above the host's admission floor: the page must come first."""
    free_at_page_gb = usage.total * (100 - volume.page_percent) / 100 / 1024**3
    if free_at_page_gb <= row.admission_floor_gb:
        raise WatermarkRefusalError(
            f"{row.name}: {volume.path} page mark {volume.page_percent}% leaves "
            f"{free_at_page_gb:.1f} GB free, not above the {row.admission_floor_gb:g} GB admission floor"
        )


def read_level(
    host_name: str, environ: Mapping[str, str] | None = None, home: Path | None = None
) -> Level | None:
    """The level in the host's reading file; None when no overlay is deployed, the host has no row or no file is written yet.

    A reading file that exists and cannot be parsed raises, so a page is never lost to a corrupt file.
    """
    try:
        overlay = load_structured_overlay(
            NODE_NAME,
            ModelLabDiskWatermarkOverlay,
            environ,
            file_env=OVERLAY_ENV,
        )
    except NodeOverlayNotFoundError:
        return None
    rows = [row for row in overlay.hosts if row.name == host_name]
    if len(rows) != 1:
        return None
    target = expand(rows[0].reading_file, home if home is not None else Path.home())
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    level = json.loads(text).get("level")
    for known in LEVEL_ORDER:
        if level == known:
            return known
    raise WatermarkRefusalError(f"{target} holds no level of ok, warn or page")


def expand(value: str, home: Path) -> Path:
    if value.startswith("$HOME/"):
        value = str(home / value[len("$HOME/") :])
    if not value.startswith("/") or ".." in Path(value).parts or "$" in value:
        raise WatermarkRefusalError(
            f"reading_file {value!r} must begin with / or $HOME/ and hold no .."
        )
    return Path(os.path.normpath(value))


def _same_filesystem(first: str, second: str) -> bool:
    try:
        return os.stat(first).st_dev == os.stat(second).st_dev
    except OSError:
        return False


class HandlerLabDiskWatermark:
    """Take one reading; ``usage``, ``now``, ``home``, the class reader and the publisher are injected so a test names its own.

    The reading file keeps the shape placement reads. When the overlay declares ``reading``, the run also measures the
    consumers by class and publishes one ``ModelHostDiskRead`` on the overlay's read topic, or one typed
    ``ModelHostDiskReadFailed`` on its failed topic (``dry_run`` measures and publishes nothing).
    """

    def __init__(
        self,
        *,
        usage: Callable[[str], Usage] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        home: Path | None = None,
        classes: HostDiskClassReader | None = None,
        publisher: ProtocolHostDiskPublisher | None = None,
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.usage = usage if usage is not None else (lambda path: disk_usage(path))
        self.now = now
        self.home = home if home is not None else Path.home()
        self.classes = classes if classes is not None else HostDiskClassReader()
        self.publisher = publisher
        self.new_id = new_id
        self.environ = environ if environ is not None else os.environ

    def handle(
        self, request: ModelLabDiskWatermarkRequest
    ) -> ModelLabDiskWatermarkReading:
        overlay = load_overlay(request.overlay_file)
        row = host_row(overlay, request.host_name)
        reading_file = expand(row.reading_file, self.home)
        settings = overlay.reading
        correlation_id = request.correlation_id or self.new_id()
        volumes: list[ModelVolumeReading] = []
        usages: list[tuple[str, Usage]] = []
        for volume in row.volumes:
            try:
                usage = self.usage(volume.path)
            except OSError as exc:
                if settings is not None and not request.dry_run:
                    self._publish_failed(
                        settings,
                        row.name,
                        correlation_id,
                        "volume_unreadable",
                        f"{volume.path} unreadable ({type(exc).__name__})",
                    )
                raise
            check_page_mark_above_floor(row, volume, usage)
            usages.append((volume.path, usage))
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
        if settings is None:
            return reading
        # The reading file is written first: the class scan walks whole caches and must never delay the level
        # placement reads (the unit's start timeout is the contract's, and is not widened for it).
        return reading.model_copy(
            update={
                "host_disk": self._host_disk(
                    row, settings, usages, correlation_id, request.dry_run
                )
            }
        )

    def _host_disk(
        self,
        row: ModelWatermarkHost,
        settings: ModelHostDiskReadingSettings,
        usages: list[tuple[str, Usage]],
        correlation_id: str,
        dry_run: bool,
    ) -> ModelHostDiskRead | ModelHostDiskReadFailed:
        classes = self.classes.read_classes(row, settings)
        read = ModelHostDiskRead(
            correlation_id=correlation_id,
            host_name=row.name,
            read_at=self.now(),
            volumes=tuple(
                ModelHostDiskVolume(
                    path=path,
                    total_bytes=usage.total,
                    free_bytes=usage.free,
                    used_bytes=usage.used,
                )
                for path, usage in usages
            ),
            classes=classes,
        )
        excess = self._class_sizes_exceed_volume(row, settings, read)
        if excess:
            return self._publish_failed(
                settings,
                row.name,
                correlation_id,
                "class_sizes_exceed_volume",
                excess,
                dry_run=dry_run,
            )
        if dry_run:
            return read
        try:
            self._publisher(settings).publish(settings.read_topic, read)
        except Exception as exc:
            return self._publish_failed(
                settings,
                row.name,
                correlation_id,
                "publish_refused",
                f"{settings.read_topic}: {type(exc).__name__}: {exc}",
            )
        return read

    def _publisher(
        self, settings: ModelHostDiskReadingSettings
    ) -> ProtocolHostDiskPublisher:
        if self.publisher is not None:
            return self.publisher
        return LaneBusHostDiskPublisher(settings.bus_lane, workspace_root(self.environ))

    def _class_sizes_exceed_volume(
        self,
        row: ModelWatermarkHost,
        settings: ModelHostDiskReadingSettings,
        read: ModelHostDiskRead,
    ) -> str:
        """The first volume whose read filesystem classes add up to more than its used bytes plus the tolerance, else ''.

        A class holds a share of a volume, so a sum above the volume's used bytes is a reading that cannot be true:
        it is refused, never published. A root on no declared volume, or on another filesystem than the volume its path
        falls under (a RAM filesystem mounted below the root volume), has nothing to be checked against and is skipped.
        """
        used = {volume.path: volume.used_bytes for volume in read.volumes}
        sums: dict[str, int] = {}
        roots: dict[str, str] = {root.name: root.path for root in row.class_roots}
        for reading in read.classes:
            if reading.class_name not in FILESYSTEM_CLASSES or reading.status != "read":
                continue
            root_path = roots[reading.class_name]
            volume = volume_for(root_path, tuple(used))
            if volume is not None and _same_filesystem(root_path, volume):
                sums[volume] = sums.get(volume, 0) + reading.total_bytes
        for volume, total in sums.items():
            allowed = used[volume] * (1 + settings.tolerance_percent / 100)
            if total > allowed:
                return (
                    f"{volume}: classes hold {total} bytes, volume used {used[volume]} bytes, "
                    f"tolerance {settings.tolerance_percent:g}%"
                )
        return ""

    def _publish_failed(
        self,
        settings: ModelHostDiskReadingSettings,
        host_name: str,
        correlation_id: str,
        reason: FailureReason,
        detail: str,
        *,
        dry_run: bool = False,
    ) -> ModelHostDiskReadFailed:
        failed = ModelHostDiskReadFailed(
            correlation_id=correlation_id,
            host_name=host_name,
            read_at=self.now(),
            reason=reason,
            detail=detail,
        )
        if dry_run:
            return failed
        try:
            self._publisher(settings).publish(settings.failed_topic, failed)
        except Exception as exc:
            return failed.model_copy(
                update={
                    "detail": f"{detail}; the failed event was not published either "
                    f"({type(exc).__name__})"
                }
            )
        return failed

    def _write(self, target: Path, reading: ModelLabDiskWatermarkReading) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=target.parent, prefix=".reading-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(reading.model_dump(exclude={"host_disk"}), indent=2)
                    + "\n"
                )
            os.replace(name, target)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise
