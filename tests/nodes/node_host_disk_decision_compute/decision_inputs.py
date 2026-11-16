# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders shared by the host disk decision tests (OMN-23866). Synthetic hosts and values only."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.model_host_disk_decision import (
    ModelHostDiskDecisionRequest,
    ModelHostDiskHostReading,
    ModelHostDiskPolicy,
)
from omnimarket.models.model_host_disk_reading import (
    DiskClassName,
    ModelDiskClassReading,
    ModelDiskGroup,
    ModelHostDiskVolume,
)

GIB = 1024**3
DAY = 86400
NOW = datetime(2026, 11, 14, 4, 0, tzinfo=UTC)
OVERLAY_EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_host_disk_decision_compute"
    / "overlay.example.yaml"
)


def example_policy_document() -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(OVERLAY_EXAMPLE.read_text())
    return document


def example_policy() -> ModelHostDiskPolicy:
    return ModelHostDiskPolicy.model_validate(example_policy_document())


def group(
    package: str,
    revision: str,
    *,
    age_days: float | None,
    bytes_: int = GIB,
    held: int = 0,
    copies: int = 1,
) -> ModelDiskGroup:
    age = None if age_days is None else int(age_days * DAY)
    return ModelDiskGroup(
        package=package,
        revision=revision,
        copies=copies,
        bytes=bytes_,
        newest_use_age_seconds=age,
        oldest_use_age_seconds=age,
        held_copies=held,
    )


def class_reading(
    class_name: DiskClassName, groups: tuple[ModelDiskGroup, ...]
) -> ModelDiskClassReading:
    return ModelDiskClassReading(
        class_name=class_name,
        status="read",
        total_bytes=sum(g.bytes for g in groups),
        entry_count=sum(g.copies for g in groups),
        revision_count=len(groups),
        groups=groups,
    )


def host_reading(
    host: str,
    *,
    free_bytes: int = 500 * GIB,
    read_at: datetime = NOW,
    classes: tuple[ModelDiskClassReading, ...] = (),
    volume_path: str = "/",
) -> ModelHostDiskHostReading:
    total = 1000 * GIB
    return ModelHostDiskHostReading(
        host_name=host,
        read_at=read_at,
        volumes=(
            ModelHostDiskVolume(
                path=volume_path,
                total_bytes=total,
                free_bytes=free_bytes,
                used_bytes=total - free_bytes,
            ),
        ),
        classes=classes,
    )


def request(
    *readings: ModelHostDiskHostReading,
    policy: ModelHostDiskPolicy | None = None,
    now: datetime = NOW,
) -> ModelHostDiskDecisionRequest:
    return ModelHostDiskDecisionRequest(
        now=now, policy=policy or example_policy(), readings=readings
    )
