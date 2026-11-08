# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""This node's own two backends and the scenario that proves them equal (OMN-22911)."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path

from omnimarket.nodes.node_two_backend_suite_effect.adapters.report_sources import (
    FileReportSource,
    InMemoryReportSource,
)
from omnimarket.nodes.node_two_backend_suite_effect.models.model_two_backend_suite import (
    ModelEffectBackendEntry,
    ModelEffectBackendReport,
)
from omnimarket.nodes.node_two_backend_suite_effect.protocols import (
    ProtocolEffectBackendReportSource,
)

SEED = ("node_seed_effect",)


def _report(names: Sequence[str]) -> ModelEffectBackendReport:
    return ModelEffectBackendReport(
        entries=tuple(ModelEffectBackendEntry(node_name=n) for n in names)
    )


def make_in_memory_backend(
    rows: Sequence[str], workdir: Path
) -> ProtocolEffectBackendReportSource:
    """The effect list the rows name, held in memory."""
    return InMemoryReportSource(_report(rows))


def make_file_backend(
    rows: Sequence[str], workdir: Path
) -> ProtocolEffectBackendReportSource:
    """The same list through a report file, as the core validator writes it."""
    path = workdir / "report.json"
    path.write_text(_report(rows).model_dump_json(), encoding="utf-8")
    return FileReportSource(path)


def scenario_report_roundtrip(
    factory: Callable[[Sequence[str], Path], ProtocolEffectBackendReportSource],
    workdir: Path,
) -> object:
    read = factory(SEED, workdir).read()
    if read.report is None:
        raise RuntimeError(read.error or "report-unreadable")
    return json.loads(read.report.model_dump_json())
