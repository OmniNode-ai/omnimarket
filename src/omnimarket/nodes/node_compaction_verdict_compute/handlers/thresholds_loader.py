# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the pre-registered thresholds data file. No number has a default."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_compaction_verdict_compute.models import (
    CompactionThresholdsError,
    ModelCompactionThresholds,
)

__all__ = ["load_thresholds"]


def load_thresholds(path: str) -> ModelCompactionThresholds:
    """Load and validate the thresholds; a missing number is refused by its field name."""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise CompactionThresholdsError("thresholds_path", str(error)) from error
    if not isinstance(raw, dict):
        raise CompactionThresholdsError("thresholds_path", "not a mapping")
    try:
        return ModelCompactionThresholds.model_validate(raw)
    except ValidationError as error:
        first = error.errors()[0]
        field = ".".join(str(part) for part in first["loc"]) or "thresholds_path"
        detail = (
            "missing from the data file"
            if first["type"] == "missing"
            else str(first["msg"])
        )
        raise CompactionThresholdsError(field, detail) from error
