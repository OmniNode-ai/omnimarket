# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two backends of the effect list: a fixed report in memory, and the validator's JSON file."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from omnimarket.nodes.node_two_backend_suite_effect.models.model_two_backend_suite import (
    ModelEffectBackendReport,
)
from omnimarket.nodes.node_two_backend_suite_effect.protocols import ModelReportRead


class InMemoryReportSource:
    """Default backend: the report it was built with."""

    def __init__(self, report: ModelEffectBackendReport) -> None:
        self._report = report

    def read(self) -> ModelReportRead:
        return ModelReportRead(report=self._report)


class FileReportSource:
    """Real backend: the report file the core validator wrote."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> ModelReportRead:
        try:
            text = self._path.read_text(encoding="utf-8")
            return ModelReportRead(
                report=ModelEffectBackendReport.model_validate_json(text)
            )
        except OSError as exc:
            return ModelReportRead(error=f"report-unreadable:{type(exc).__name__}")
        except ValidationError:
            return ModelReportRead(error="report-unreadable:ValidationError")
