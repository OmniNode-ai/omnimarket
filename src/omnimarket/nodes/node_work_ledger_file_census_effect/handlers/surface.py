# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which surface a counted file sits on, and the daily record built from a census run.

Runtime code is a file under ``src/`` and a runtime contract (``contract.yaml``). Everything else a reader of the ledger
file can sit in is counted apart: tests (a ``tests`` or ``test`` directory, a ``test_*`` or ``*_test`` module), docs (a
``docs`` directory or a document suffix), plan contracts (``contracts/<ticket>.yaml``) and data (the rest: benchmarks,
fixtures, scripts, configuration, workflows). The first matching rule wins, in that order, so a fixture under
``src/.../tests/`` is a test and not runtime code.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import PurePosixPath

from omnimarket.nodes.node_work_ledger_file_census_effect.models.model_census_daily_record import (
    EnumCensusSurface,
    ModelCensusDailyRecord,
    ModelCensusRepositoryRecord,
    ModelCensusSurfaceFiles,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.models.model_file_census import (
    ModelCensusHit,
    ModelRepositoryCount,
)

_TEST_DIRS = frozenset({"tests", "test", "__tests__", "fixtures"})
_DOC_DIRS = frozenset({"docs", "doc"})
_DOC_SUFFIXES = frozenset({".md", ".rst", ".txt", ".adoc"})


def classify_surface(path: str) -> EnumCensusSurface:
    """The surface of one repository-relative path."""
    pure = PurePosixPath(path)
    parts = pure.parts
    directories = parts[:-1]
    name = pure.name
    if (
        any(part in _TEST_DIRS for part in directories)
        or name.startswith("test_")
        or pure.stem.endswith("_test")
        or name == "conftest.py"
    ):
        return EnumCensusSurface.TESTS
    if directories[:1] == ("contracts",) and pure.suffix in {".yaml", ".yml"}:
        return EnumCensusSurface.PLAN_CONTRACT
    if any(part in _DOC_DIRS for part in directories) or pure.suffix in _DOC_SUFFIXES:
        return EnumCensusSurface.DOCS
    if "src" in directories or name == "contract.yaml":
        return EnumCensusSurface.RUNTIME
    return EnumCensusSurface.DATA


def surface_files(hits: Iterable[ModelCensusHit]) -> ModelCensusSurfaceFiles:
    """Distinct counted files per surface."""
    held: dict[EnumCensusSurface, set[str]] = {
        surface: set() for surface in EnumCensusSurface
    }
    for hit in hits:
        held[classify_surface(hit.path)].add(hit.path)
    return ModelCensusSurfaceFiles(
        runtime=len(held[EnumCensusSurface.RUNTIME]),
        tests=len(held[EnumCensusSurface.TESTS]),
        docs=len(held[EnumCensusSurface.DOCS]),
        data=len(held[EnumCensusSurface.DATA]),
        plan_contract=len(held[EnumCensusSurface.PLAN_CONTRACT]),
    )


def build_daily_record(
    *,
    correlation_id: str,
    clock: Callable[[], datetime],
    passed: bool,
    failure_code: str | None,
    counts: Iterable[ModelRepositoryCount],
) -> ModelCensusDailyRecord:
    """One typed record of a run: every counted repository with its surfaces, the total and the runtime-code count."""
    rows = tuple(
        ModelCensusRepositoryRecord(
            repository=count.repository,
            own_line=count.own_line,
            state=count.state,
            files=count.files,
            readers=count.readers,
            writers=count.writers,
            ratchet=count.ratchet,
            surfaces=surface_files(count.hits),
        )
        for count in counts
    )
    counted = [row for row in rows if not row.own_line]
    return ModelCensusDailyRecord(
        correlation_id=correlation_id,
        recorded_at=clock(),
        passed=passed,
        failure_code=failure_code,
        repositories=rows,
        total_files=sum(row.files for row in counted),
        runtime_files=sum(row.surfaces.runtime for row in counted),
    )
