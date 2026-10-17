# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deployment overlay of the lab history archive: hosts and roles, sources, staging, Drive.

Resolved like the disk hygiene overlay: an explicit pointer wins, otherwise the first
`node_lab_history_archive_effect/overlay.yaml` under ONEX_SKILL_OVERLAY_ROOTS. A missing
overlay is None; an invalid one raises, naming only the exception type.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import ValidationError

from .models import ModelLabHistoryArchiveDeployment

OVERLAY_ENV = "OMNIMARKET_LAB_HISTORY_ARCHIVE_OVERLAY"
NODE_DIR = "node_lab_history_archive_effect"


class LabHistoryArchiveConfigurationError(ValueError):
    """Missing or invalid deployment configuration."""


def resolve_overlay_path(explicit: Path | None = None) -> Path | None:
    if explicit is not None:
        return explicit
    if OVERLAY_ENV in os.environ:
        pointer = os.environ[OVERLAY_ENV]
        if not pointer:
            raise LabHistoryArchiveConfigurationError(
                f"{OVERLAY_ENV}: empty overlay pointer"
            )
        return Path(pointer)
    for root in os.environ.get("ONEX_SKILL_OVERLAY_ROOTS", "").split(os.pathsep):
        if not root:
            continue
        candidate = Path(root) / NODE_DIR / "overlay.yaml"
        if candidate.exists():
            return candidate
    return None


def load_deployment_overlay(
    explicit: Path | None = None,
) -> ModelLabHistoryArchiveDeployment | None:
    path = resolve_overlay_path(explicit)
    if path is None:
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("overlay must be a mapping")
        return ModelLabHistoryArchiveDeployment.model_validate(raw)
    except (OSError, UnicodeError, yaml.YAMLError, ValueError, ValidationError) as exc:
        raise LabHistoryArchiveConfigurationError(
            f"{path.name}: invalid deployment overlay ({type(exc).__name__})"
        ) from None
