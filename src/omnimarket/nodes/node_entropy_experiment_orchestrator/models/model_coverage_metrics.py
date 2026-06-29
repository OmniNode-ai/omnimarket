# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Structured coverage evidence value object for entropy track results (OMN-13614).

Absorbed from the SEA ``entropy_comparison/coverage.py`` module as part of the
SEA->canonical migration (epic OMN-13604). Only the **pure** surface is absorbed:
the ``ModelCoverageMetrics`` evidence value object and the pure
``parse_coverage_json`` reader.

The SEA original also shipped ``run_generated_code_coverage``, which shells out to
``pytest --cov`` in a sandbox. That is subprocess I/O and therefore cannot live on
the deterministic, no-I/O orchestrator path. The orchestrator consumes coverage
**results** that callers supply as fixture/replay inputs; running coverage is an
EFFECT concern handled outside this node. ``parse_coverage_json`` is retained as a
pure reader for callers that already have a coverage.py JSON artifact on disk.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_entropy_experiment_orchestrator.models.model_entropy_failure import (
    EntropyFailureClass,
)

__all__ = [
    "CoverageJsonError",
    "CoverageStatus",
    "ModelCoverageMetrics",
    "parse_coverage_json",
]

CoverageStatus = Literal["not_run", "succeeded", "failed"]


class CoverageJsonError(ValueError):
    """Raised when pytest-cov JSON output is absent or malformed."""

    failure_class = EntropyFailureClass.COVERAGE_FAILED


class ModelCoverageMetrics(BaseModel):
    """Evidence metadata from a generated-code coverage run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: CoverageStatus
    tests_discovered: int = Field(ge=0)
    coverage_pct: float | None = Field(default=None, ge=0, le=100)
    coverage_json_path: str = ""
    coverage_results_path: str = ""
    command: tuple[str, ...] = Field(default_factory=tuple)
    returncode: int | None = None
    failure_class: EntropyFailureClass | None = None
    failure_message: str = ""


def parse_coverage_json(path: Path) -> float:
    """Return total coverage percentage from coverage.py JSON output (pure read)."""
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise CoverageJsonError(f"coverage JSON missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CoverageJsonError(f"coverage JSON is malformed: {exc.msg}") from exc

    if not isinstance(payload, dict):
        raise CoverageJsonError("coverage JSON must be an object")
    totals = payload.get("totals")
    if not isinstance(totals, dict):
        raise CoverageJsonError("coverage JSON missing totals object")

    raw_percent = totals.get("percent_covered")
    if isinstance(raw_percent, bool) or not isinstance(raw_percent, int | float):
        raise CoverageJsonError("coverage JSON totals.percent_covered must be numeric")

    coverage_pct = float(raw_percent)
    if coverage_pct < 0 or coverage_pct > 100:
        raise CoverageJsonError(
            "coverage JSON totals.percent_covered must be between 0 and 100"
        )
    return coverage_pct
