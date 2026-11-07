# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocols of the job-start source's seams: the reader, the retained store and the HTTP getter."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from omnimarket.nodes.node_job_start_source_effect.models.model_job_start import (
    ModelJobStartRead,
    ModelJobStartResult,
    ModelJobStartStoreOutcome,
)


class ProtocolJobStartReader(Protocol):
    """Reads the jobs that started in ``[since, until]`` in every named repository."""

    def read(
        self,
        *,
        repositories: tuple[str, ...],
        since: datetime,
        until: datetime,
        now: datetime,
    ) -> ModelJobStartRead: ...


class ProtocolJobStartStore(Protocol):
    """The retained store: keeps one read per row key; the same key again is not written twice."""

    def append(self, result: ModelJobStartResult) -> ModelJobStartStoreOutcome: ...


class ProtocolHttpGet(Protocol):
    def get(
        self, url: str, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]: ...


__all__ = ["ProtocolHttpGet", "ProtocolJobStartReader", "ProtocolJobStartStore"]
