# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Durable heartbeat projection and once-per-command route decisions (OMN-21402).

Different runtime routes construct different handler instances; rebuilds replace
the process. Keep both health and decisions beside the existing publish record.
Failure to read or persist a selection refuses publication. It must never fall
back to a fixed host, or select a different target for a possibly published copy.
"""

from __future__ import annotations

import fcntl
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_dev_deploy_route import (
    ModelDevDeployHealth,
    ModelDevDeployRoute,
    ModelDevDeployRoutingPolicy,
    select_dev_deploy_route,
)


class _Decision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    request_fingerprint: str
    route: ModelDevDeployRoute


class _Document(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = Field(default=1, ge=1, le=1, strict=True)
    health: list[ModelDevDeployHealth] = Field(default_factory=list)
    decisions: dict[str, _Decision] = Field(default_factory=dict)


class DevDeployRoutingStore:
    """Serialize updates and persist each selected target before any publish."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.policy = ModelDevDeployRoutingPolicy.model_validate(
            yaml.safe_load(
                (Path(__file__).parents[1] / "routing_policy.yaml").read_text()
            )
        )

    @contextmanager
    def _transaction(self) -> Iterator[_Document]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                document = (
                    _Document.model_validate_json(self.path.read_text())
                    if self.path.exists()
                    else _Document()
                )
                yield document
                descriptor, temporary = tempfile.mkstemp(
                    dir=self.path.parent, prefix=".dev-deploy-route-"
                )
                try:
                    with os.fdopen(descriptor, "w") as handle:
                        handle.write(document.model_dump_json())
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, self.path)
                finally:
                    Path(temporary).unlink(missing_ok=True)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def record(self, reading: ModelDevDeployHealth) -> None:
        """Keep the newest heartbeat; equal conflicting observations refuse."""
        with self._transaction() as document:
            key = (reading.instance, reading.repository)
            rows = {(row.instance, row.repository): row for row in document.health}
            old = rows.get(key)
            if old is not None:
                if reading.observed_at < old.observed_at:
                    return
                if reading.observed_at == old.observed_at and reading != old:
                    raise ValueError(
                        "conflicting dev deploy heartbeats at one timestamp"
                    )
            rows[key] = reading
            document.health = sorted(
                rows.values(), key=lambda row: (row.observed_at, row.instance)
            )[-self.policy.max_health_rows :]

    def choose(
        self,
        correlation_id: UUID,
        *,
        request_fingerprint: str,
        repository: str,
        now: datetime,
    ) -> ModelDevDeployRoute:
        """A retry retains its target even if publication may have succeeded.

        A new command can choose another host. Moving an already accepted job
        requires fencing at the executor, not a second health-based selection.
        """
        with self._transaction() as document:
            key = str(correlation_id)
            previous = document.decisions.get(key)
            if previous is not None:
                if previous.request_fingerprint != request_fingerprint:
                    raise ValueError("a dev deploy correlation names two requests")
                return previous.route
            route = select_dev_deploy_route(
                tuple(document.health),
                repository=repository,
                now=now,
                policy=self.policy,
            )
            document.decisions[key] = _Decision(
                request_fingerprint=request_fingerprint, route=route
            )
            while len(document.decisions) > self.policy.max_decisions:
                del document.decisions[next(iter(document.decisions))]
            return route
