# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The daily record of the ledger-file census: per-repository counts and the runtime-code count as one typed record.

One record per run. It carries, for every counted repository, the files that hold an executable reader or writer of the
ledger file, split by surface: runtime code (``src/`` and runtime contracts) apart from tests, docs, data and plan
contracts. The runtime-code count is the number the operator's goal drives to zero.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumCensusSurface(StrEnum):
    """Where a counted file sits. Only ``runtime`` is runtime code."""

    RUNTIME = "runtime"
    TESTS = "tests"
    DOCS = "docs"
    DATA = "data"
    PLAN_CONTRACT = "plan_contract"


class ModelCensusSurfaceFiles(_Frozen):
    """Files per surface; their sum is the repository's file count."""

    runtime: int = Field(ge=0)
    tests: int = Field(ge=0)
    docs: int = Field(ge=0)
    data: int = Field(ge=0)
    plan_contract: int = Field(ge=0)

    @property
    def total(self) -> int:
        return self.runtime + self.tests + self.docs + self.data + self.plan_contract


class ModelCensusRepositoryRecord(_Frozen):
    repository: str = Field(min_length=1)
    own_line: bool
    state: str
    files: int = Field(ge=0)
    readers: int = Field(ge=0)
    writers: int = Field(ge=0)
    ratchet: int | None = None
    surfaces: ModelCensusSurfaceFiles


class ModelCensusDailyRecord(_Frozen):
    correlation_id: str = Field(min_length=1)
    recorded_at: datetime
    passed: bool
    failure_code: str | None = None
    repositories: tuple[ModelCensusRepositoryRecord, ...]
    total_files: int = Field(
        ge=0, description="Files in repositories that are not on their own line."
    )
    runtime_files: int = Field(
        ge=0,
        description="Runtime-code files (src/ and runtime contracts) in repositories that are not on their own line.",
    )

    def counters(self) -> dict[str, int]:
        """The record as the emission chain's integer counters: totals, then one pair per repository."""
        counters: dict[str, int] = {
            "files": self.total_files,
            "repositories": len(self.repositories),
            "runtime_files": self.runtime_files,
        }
        for repo in self.repositories:
            counters[f"files:{repo.repository}"] = repo.files
            counters[f"runtime:{repo.repository}"] = repo.surfaces.runtime
        return counters
