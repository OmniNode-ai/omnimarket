# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared test support of node_job_start_source_effect (OMN-22897): synthetic data, fakes and a fixture overlay."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import yaml

from omnimarket.nodes.node_job_start_source_effect.models.model_job_start import (
    EnumJobStartFailure,
    ModelJobStart,
    ModelJobStartRead,
)
from omnimarket.nodes.node_job_start_source_effect.overlay import (
    ModelJobStartOverlay,
)

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_job_start_source_effect"
)
REPO_A = "example-org/example-repo"
REPO_B = "example-org/example-other"
GROUP = "example-private-group"
TOKEN_ENV = "EXAMPLE_JOB_READ_TOKEN"
TOKEN_VALUE = "synthetic-token-value-0001"


def example_fields(name: str = "overlay.example.yaml") -> dict[str, Any]:
    return copy.deepcopy(yaml.safe_load((NODE_DIR / name).read_text(encoding="utf-8")))


def overlay(
    name: str = "overlay.example.yaml", **settings: Any
) -> ModelJobStartOverlay:
    fields = example_fields(name)
    fields["settings"].update(settings)
    return ModelJobStartOverlay.model_validate(fields)


def start(
    *,
    repository: str = REPO_A,
    job_id: int = 5001,
    run_id: int = 1001,
    started_at: str = "2026-11-07T11:30:00Z",
    group: str | None = GROUP,
    fork: bool = False,
    private: bool = True,
    event: str = "push",
) -> ModelJobStart:
    return ModelJobStart.model_validate(
        {
            "repository": repository,
            "run_id": run_id,
            "job_id": job_id,
            "job_name": "build",
            "workflow_name": "ci",
            "run_event": event,
            "head_is_fork": fork,
            "repository_private": private,
            "runner_group_name": group,
            "runner_name": "example-runner-1",
            "labels": ["example-label"],
            "started_at": started_at,
        }
    )


class FakeReader:
    """An injected reader: returns what it is given, or raises what it is given."""

    def __init__(
        self,
        read: ModelJobStartRead | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self._read = read
        self._raises = raises
        self.requests: list[dict[str, Any]] = []

    def read(
        self,
        *,
        repositories: tuple[str, ...],
        since: datetime,
        until: datetime,
        now: datetime,
    ) -> ModelJobStartRead:
        self.requests.append(
            {
                "repositories": repositories,
                "since": since,
                "until": until,
                "now": now,
            }
        )
        if self._raises is not None:
            raise self._raises
        assert self._read is not None
        return self._read


def good_read(*starts: ModelJobStart, age_seconds: int = 60) -> ModelJobStartRead:
    from datetime import timedelta

    return ModelJobStartRead(
        starts=starts,
        repositories_read=(REPO_A, REPO_B),
        read_at=NOW - timedelta(seconds=age_seconds),
        calls=3,
    )


def failed_read(
    failure: EnumJobStartFailure, repository: str = REPO_A
) -> ModelJobStartRead:
    return ModelJobStartRead(
        read_at=NOW, calls=1, read_failure=failure, failed_repository=repository
    )


Route = Callable[[Mapping[str, str]], tuple[int, bytes]]


class FakeHttp:
    """An injected HTTP getter over routes keyed by URL path; records every call."""

    def __init__(self, routes: Mapping[str, Route]) -> None:
        self._routes = dict(routes)
        self.calls: list[tuple[str, dict[str, str], Mapping[str, str]]] = []

    def get(
        self, url: str, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]:
        parts = urlsplit(url)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.calls.append((parts.path, query, dict(headers)))
        route = self._routes.get(parts.path)
        if route is None:
            return 404, b"{}"
        return route(query)


def json_route(body: object, status: int = 200) -> Route:
    payload = json.dumps(body).encode("utf-8")
    return lambda _query: (status, payload)


def run_item(
    run_id: int, *, updated_at: str = "2026-11-07T11:45:00Z", **extra: Any
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": run_id,
        "event": "push",
        "name": "ci",
        "status": "completed",
        "updated_at": updated_at,
        "repository": {"full_name": REPO_A, "private": True},
        "head_repository": {"full_name": REPO_A},
    }
    item.update(extra)
    return item


def job_item(
    job_id: int, run_id: int, *, started_at: str | None, group: str | None = GROUP
) -> dict[str, Any]:
    return {
        "id": job_id,
        "run_id": run_id,
        "name": "build",
        "status": "completed",
        "started_at": started_at,
        "labels": ["example-label"],
        "runner_name": "example-runner-1",
        "runner_group_name": group,
    }
