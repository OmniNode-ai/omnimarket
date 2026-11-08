# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reader the filler injects: one call fetches one job's log into the cache."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.models.model_github_gateway_io import ModelJobLogTailResult


class JobLogCacheMissError(Exception):
    """A cache-only probe found no cached log for the job."""


@runtime_checkable
class ProtocolJobLogReader(Protocol):
    """Read one job's log through the gateway's job-log cache.

    ``cache_only=True`` probes the cache and raises :class:`JobLogCacheMissError` on a
    miss, making no GitHub call. ``cache_only=False`` downloads the log into the cache
    when it is absent. Any other failure raises and is recorded against the run.
    """

    cache_path: str | None

    def read(
        self, repo: str, job_id: int, *, cache_only: bool
    ) -> ModelJobLogTailResult: ...


__all__ = ["JobLogCacheMissError", "ProtocolJobLogReader"]
