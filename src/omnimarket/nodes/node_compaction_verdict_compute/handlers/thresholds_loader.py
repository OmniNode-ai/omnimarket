# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parse the pre-registered thresholds data file. No number has a default.

The node performs no I/O: the caller reads the data file and passes its text in
the request beside the path it came from, and this module parses that text.
"""

from __future__ import annotations

import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_compaction_verdict_compute.models import (
    CompactionThresholdsError,
    ModelCompactionThresholds,
)

__all__ = ["parse_thresholds"]


def parse_thresholds(text: str) -> ModelCompactionThresholds:
    """Parse and validate the thresholds; a missing number is refused by its field name."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise CompactionThresholdsError("thresholds_text", str(error)) from error
    if not isinstance(raw, dict):
        raise CompactionThresholdsError("thresholds_text", "not a mapping")
    try:
        return ModelCompactionThresholds.model_validate(raw)
    except ValidationError as error:
        first = error.errors()[0]
        field = ".".join(str(part) for part in first["loc"]) or "thresholds_text"
        detail = (
            "missing from the data file"
            if first["type"] == "missing"
            else str(first["msg"])
        )
        raise CompactionThresholdsError(field, detail) from error
