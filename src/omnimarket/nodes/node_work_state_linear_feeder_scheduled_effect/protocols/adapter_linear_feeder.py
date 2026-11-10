# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live profile: the linear snapshot node writes its newest snapshot where the projection reads its tickets, mapped to the feeder's outcome (OMN-23427)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from omnimarket.models.work_state_feeder import ModelFeederOutcome
from omnimarket.nodes.node_linear_snapshot_effect.handlers.handler_linear_snapshot import (
    HandlerLinearSnapshot,
)
from omnimarket.nodes.node_linear_snapshot_effect.models.model_linear_snapshot import (
    ModelLinearSnapshotRequest,
    ModelLinearSnapshotResult,
)
from omnimarket.nodes.node_work_state_projection_effect.runtime_work_state import (
    runtime_section,
)

#: When the snapshot run failed, the outcome's cause when the run itself named none.
FAILED_CAUSE = "linear snapshot failed"


class LinearSnapshotFeeder:
    """One feed: run the linear snapshot node against the projection's linear directory.

    The directory is never a literal here: it is the projection contract's ``runtime.linear.dir_env``
    when the environment names one, else the home env's path under ``runtime.linear.dir_relpath``.
    The outcome's ``source_as_of`` is the time of the newest fact, not the write's: a ``stale`` run
    kept an older snapshot, so its age is taken off the tick time.
    """

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        handler: Any = None,
    ) -> None:
        self._environ = environ
        self._handler = handler

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None) -> LinearSnapshotFeeder:
        """The live profile's feeder, bound to the process's environment."""
        return cls(environ=environ)

    def _out_dir(self, env: Mapping[str, str]) -> str:
        rt = runtime_section()
        named = env.get(rt["linear"]["dir_env"], "").strip()
        if named:
            return named
        return str(Path(env[rt["home_env"]]) / rt["linear"]["dir_relpath"])

    def feed(self, now: datetime) -> ModelFeederOutcome:
        env = os.environ if self._environ is None else self._environ
        result = (self._handler or HandlerLinearSnapshot()).handle(
            ModelLinearSnapshotRequest(out_dir=self._out_dir(env))
        )
        if result.status == "written":
            return ModelFeederOutcome(
                status="fed", source_as_of=now, item_count=result.issue_count
            )
        if result.status == "stale":
            return ModelFeederOutcome(
                status="fed",
                source_as_of=now - timedelta(seconds=result.age_s or 0),
                item_count=result.issue_count,
                cause=result.reason,
            )
        return self._failed(result)

    def _failed(self, result: ModelLinearSnapshotResult) -> ModelFeederOutcome:
        return ModelFeederOutcome(
            status="failed",
            item_count=0,
            cause=result.reason or FAILED_CAUSE,
        )


__all__ = ["LinearSnapshotFeeder"]
