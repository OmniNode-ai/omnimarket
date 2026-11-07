# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two backends of the effect list: a fixed report in memory, and the validator's JSON file."""

from __future__ import annotations

import os
from collections.abc import Mapping
from functools import singledispatch
from pathlib import Path

from pydantic import ValidationError

from omnimarket.nodes.node_two_backend_suite_effect.models.model_two_backend_suite import (
    ModelEffectBackendReport,
)
from omnimarket.nodes.node_two_backend_suite_effect.overlay import (
    ModelFileReportSettings,
    ModelInMemoryReportSettings,
)
from omnimarket.nodes.node_two_backend_suite_effect.protocols import (
    ModelReportRead,
    ProtocolEffectBackendReportSource,
)


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


class ReportRouteError(RuntimeError):
    """A report route the overlay names is not configured; the message names the variable."""


@singledispatch
def build_report_source(
    settings: object, environ: Mapping[str, str] | None = None
) -> ProtocolEffectBackendReportSource:
    """Build the backend selected and validated by the overlay's tagged schema."""
    raise ReportRouteError(
        f"unsupported report configuration: {type(settings).__name__}"
    )


@build_report_source.register
def _in_memory_source(
    settings: ModelInMemoryReportSettings, environ: Mapping[str, str] | None = None
) -> ProtocolEffectBackendReportSource:
    return InMemoryReportSource(ModelEffectBackendReport())


@build_report_source.register
def _file_source(
    settings: ModelFileReportSettings, environ: Mapping[str, str] | None = None
) -> ProtocolEffectBackendReportSource:
    env = os.environ if environ is None else environ
    path = env.get(settings.path_env)
    if not path:
        raise ReportRouteError(
            f"{settings.path_env} is not set (overlay report.path_env)"
        )
    return FileReportSource(Path(path))
