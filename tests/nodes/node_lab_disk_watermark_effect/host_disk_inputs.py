# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Inputs shared by the host disk reading's tests (OMN-23865): a made-up host with real directories under a tmp path."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.events.topics import (
    HOST_DISK_READ_FAILED_TOPIC_V1,
    HOST_DISK_READ_TOPIC_V1,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.host_disk_class_reader import (
    HostDiskClassReader,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.handler_lab_disk_watermark import (
    HandlerLabDiskWatermark,
    Usage,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.host_disk_ports import (
    ProtocolHostDiskPublisher,
)
from omnimarket.nodes.node_temp_retention_effect.handlers.handler_temp_retention import (
    ProcessReferences,
)

GIB = 1024**3
NOW = datetime(2026, 11, 11, 21, 0, 0, tzinfo=UTC)
READ_TOPIC = HOST_DISK_READ_TOPIC_V1
FAILED_TOPIC = HOST_DISK_READ_FAILED_TOPIC_V1
DF_LINES = "\n".join(
    json.dumps(row)
    for row in (
        {"Type": "Images", "TotalCount": "4", "Size": "2.5GB"},
        {"Type": "Local Volumes", "TotalCount": "2", "Size": "1GB"},
        {"Type": "Build Cache", "TotalCount": "9", "Size": "300MB"},
    )
)
EXITED_LINES = "\n".join(
    json.dumps(row)
    for row in (
        {"Names": "old-a", "Image": "img:1", "Size": "10MB (virtual 1.2GB)"},
        {"Names": "old-b", "Image": "img:1", "Size": "20MB (virtual 1.2GB)"},
    )
)


def make_entry(
    root: Path, name: str, package: str, version: str, *, age_seconds: int, payload: int
) -> Path:
    """A uv-cache entry: a hash directory holding ``<package>/`` and ``<package>-<version>.dist-info/``."""
    entry = root / name
    (entry / package.replace("-", "_")).mkdir(parents=True)
    (entry / f"{package.replace('-', '_')}-{version}.dist-info").mkdir()
    (entry / package.replace("-", "_") / "data.bin").write_bytes(b"x" * payload)
    stamp = NOW.timestamp() - age_seconds
    for path in (*entry.rglob("*"), entry):
        os.utime(path, (stamp, stamp))
    return entry


def make_host(tmp_path: Path) -> dict[str, Path]:
    """Three omnimarket revisions of the lane package cache (3 copies of 1.0, 1 of 2.0), a worktree and a lab-run folder."""
    uv = tmp_path / "uv-cache"
    archive = uv / "archive-v0"
    for index, age in enumerate((7200, 3600, 1800)):
        make_entry(
            archive, f"h1{index}", "omnimarket", "1.0", age_seconds=age, payload=4096
        )
    make_entry(archive, "h2", "omnimarket", "2.0", age_seconds=60, payload=4096)
    make_entry(archive, "h3", "requests", "2.31", age_seconds=90000, payload=1024)
    worktrees = tmp_path / "worktrees"
    (worktrees / "OMN-1").mkdir(parents=True)
    (worktrees / "OMN-1" / "f").write_bytes(b"y" * 2048)
    lab_run = tmp_path / "lab-run"
    (lab_run / "run-a").mkdir(parents=True)
    (lab_run / "run-a" / "log").write_bytes(b"z" * 512)
    return {"uv": uv, "worktrees": worktrees, "lab_run": lab_run}


def overlay_document(
    paths: dict[str, Path],
    *,
    classes: Sequence[str] = ("uv_cache", "worktrees", "lab_run"),
    docker: bool = True,
    tolerance_percent: float = 2,
    reading: bool = True,
    interval_seconds: int = 900,
) -> dict[str, object]:
    roots = {
        "uv_cache": {
            "name": "uv_cache",
            "path": str(paths["uv"]),
            "entries_subdir": "archive-v0",
        },
        "worktrees": {"name": "worktrees", "path": str(paths["worktrees"])},
        "lab_run": {"name": "lab_run", "path": str(paths["lab_run"])},
    }
    host: dict[str, object] = {
        "name": "pool-a",
        "scheduler": "systemd-user",
        "reading_file": "$HOME/state/pool-a.json",
        "admission_floor_gb": 5,
        "volumes": [
            {"path": str(paths["uv"].parent), "warn_percent": 80, "page_percent": 90}
        ],
        "class_roots": [roots[name] for name in classes],
    }
    if docker:
        host["docker"] = {
            "df_command": ["docker", "system", "df"],
            "exited_command": ["docker", "ps", "-a"],
            "timeout_seconds": 30,
        }
    document: dict[str, object] = {"hosts": [host]}
    if reading:
        document["reading"] = {
            "tolerance_percent": tolerance_percent,
            "bus_lane": "dev",
            "read_topic": READ_TOPIC,
            "failed_topic": FAILED_TOPIC,
            "max_groups_per_class": 50,
            "interval_seconds": interval_seconds,
        }
    return document


def write_overlay(tmp_path: Path, document: dict[str, object]) -> Path:
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(document))
    return path


def fake_docker(command: Sequence[str], timeout: int) -> str:
    return DF_LINES if "system" in command else EXITED_LINES


def handler(
    tmp_path: Path,
    publisher: ProtocolHostDiskPublisher,
    *,
    used_bytes: int = 50 * GIB,
    held: Sequence[Path] = (),
) -> HandlerLabDiskWatermark:
    refs = ProcessReferences(directories=list(held), blobs=[], commands=[])
    return HandlerLabDiskWatermark(
        usage=lambda _path: Usage(100 * GIB, used_bytes, 100 * GIB - used_bytes),
        now=lambda: NOW,
        home=tmp_path,
        classes=HostDiskClassReader(
            now=NOW.timestamp,
            process_references=lambda: refs,
            run_docker=fake_docker,
        ),
        publisher=publisher,
        new_id=lambda: "corr-1",
    )
