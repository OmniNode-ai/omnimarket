# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real job-start reader: reads the jobs that started through the GitHub REST API.

For each declared repository it lists the workflow runs created since the lookback the overlay sets,
skips the runs last updated before the window opened (none of their jobs can have started in it), and
lists the jobs of each remaining run. A job is a start when it carries a ``started_at`` inside the
window. Every call is counted against the overlay's ``max_calls``; the call past it is not made and
the read ends in ``budget-exhausted``. Reads only (GET). The token comes from the environment
variable the overlay names, at call time, and is never put in a result, an error or a log line.

Every way the read can fail is a typed failure and none returns the starts found so far as if they
were all: a rate limit, a denial, a broken body, a call budget and a listing with more pages than the
overlay allows (``window-truncated``) all end the read with no starts.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from omnimarket.nodes.node_job_start_source_effect.models.model_job_start import (
    EnumJobStartFailure,
    JobStartReaderError,
    ModelJobStart,
    ModelJobStartRead,
)
from omnimarket.nodes.node_job_start_source_effect.overlay import ModelGithubSource
from omnimarket.nodes.node_job_start_source_effect.protocols.protocol_job_start import (
    ProtocolHttpGet,
)

_DENIED = (401, 403, 404)
_RATE_LIMITED = 429


def _time(text: object) -> datetime:
    if not isinstance(text, str) or not text:
        raise ValueError("no time")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class GithubJobStartReader:
    def __init__(
        self,
        source: ModelGithubSource,
        http_get: ProtocolHttpGet,
        environ: Mapping[str, str],
        *,
        run_lookback_seconds: int,
    ) -> None:
        self._s = source
        self._http = http_get
        self._environ = environ
        self._lookback = run_lookback_seconds
        self._calls = 0

    # -- transport -----------------------------------------------------------------------
    def _get(self, path: str, repository: str) -> Any:
        if self._calls >= self._s.max_calls:
            raise JobStartReaderError(
                EnumJobStartFailure.BUDGET_EXHAUSTED, repository, self._calls
            )
        token = self._environ.get(self._s.token_env, "")
        if not token:
            raise JobStartReaderError(
                EnumJobStartFailure.ORG_READ_DENIED, repository, self._calls
            )
        self._calls += 1
        status, body = self._http.get(
            f"{self._s.api_base.rstrip('/')}{path}",
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            self._s.timeout_seconds,
        )
        if status == _RATE_LIMITED or (status == 403 and b"rate limit" in body.lower()):
            raise JobStartReaderError(
                EnumJobStartFailure.RATE_LIMITED, repository, self._calls
            )
        if status in _DENIED:
            raise JobStartReaderError(
                EnumJobStartFailure.ORG_READ_DENIED, repository, self._calls
            )
        if status != 200:
            raise JobStartReaderError(
                EnumJobStartFailure.UNREADABLE, repository, self._calls
            )
        try:
            return json.loads(body)
        except ValueError as error:
            raise JobStartReaderError(
                EnumJobStartFailure.UNREADABLE, repository, self._calls
            ) from error

    def _items(self, base: str, query: str, key: str, repository: str) -> list[Any]:
        """Every item of a paged listing; more pages than the overlay allows is a typed failure."""
        size = self._s.page_size
        items: list[Any] = []
        for page in range(1, self._s.max_pages_per_repository + 1):
            body = self._get(f"{base}?{query}&per_page={size}&page={page}", repository)
            listing = body.get(key) if isinstance(body, dict) else None
            if not isinstance(listing, list):
                raise JobStartReaderError(
                    EnumJobStartFailure.UNREADABLE, repository, self._calls
                )
            items.extend(listing)
            if len(listing) < size:
                return items
            total = body.get("total_count")
            if (
                isinstance(total, int)
                and not isinstance(total, bool)
                and len(items) >= total
            ):
                return items  # the final page was exactly full and the listing says nothing follows
        raise JobStartReaderError(
            EnumJobStartFailure.WINDOW_TRUNCATED, repository, self._calls
        )

    # -- parsing -------------------------------------------------------------------------
    def _starts_of_run(
        self,
        repository: str,
        run: Mapping[str, Any],
        since: datetime,
        until: datetime,
    ) -> list[ModelJobStart]:
        run_id = int(run["id"])
        head = (run.get("head_repository") or {}).get("full_name")
        base = (run.get("repository") or {}).get("full_name")
        private = bool((run.get("repository") or {}).get("private", True))
        jobs = self._items(
            f"/repos/{repository}/actions/runs/{run_id}/jobs",
            "filter=all",
            "jobs",
            repository,
        )
        starts: list[ModelJobStart] = []
        for job in jobs:
            started = job.get("started_at")
            if started is None:
                continue
            moment = _time(started)
            if not since <= moment <= until:
                continue
            starts.append(
                ModelJobStart(
                    repository=repository,
                    run_id=run_id,
                    job_id=int(job["id"]),
                    job_name=str(job.get("name", "")),
                    workflow_name=str(run.get("name") or ""),
                    run_event=str(run["event"]),
                    # A run that names no head repository is not shown to be the repository's own: it counts as a fork.
                    head_is_fork=not (head and base and head == base),
                    repository_private=private,
                    runner_group_name=job.get("runner_group_name"),
                    runner_name=job.get("runner_name"),
                    labels=tuple(str(label) for label in job.get("labels") or ()),
                    started_at=moment,
                )
            )
        return starts

    # -- the read ------------------------------------------------------------------------
    def read(
        self,
        *,
        repositories: tuple[str, ...],
        since: datetime,
        until: datetime,
        now: datetime,
    ) -> ModelJobStartRead:
        self._calls = 0
        created_since = since - timedelta(seconds=self._lookback)
        created = quote(f">={_stamp(created_since)}", safe="")
        starts: list[ModelJobStart] = []
        done: list[str] = []
        current: str | None = None
        try:
            for repository in repositories:
                current = repository
                runs = self._items(
                    f"/repos/{repository}/actions/runs",
                    f"created={created}",
                    "workflow_runs",
                    repository,
                )
                for run in runs:
                    if _time(run["updated_at"]) < since:
                        continue
                    starts.extend(self._starts_of_run(repository, run, since, until))
                done.append(repository)
        except JobStartReaderError as stop:
            return ModelJobStartRead(
                read_at=now,
                calls=self._calls,
                read_failure=stop.failure,
                failed_repository=stop.repository,
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            return ModelJobStartRead(
                read_at=now,
                calls=self._calls,
                read_failure=EnumJobStartFailure.UNREADABLE,
                failed_repository=current,
            )
        return ModelJobStartRead(
            starts=tuple(starts),
            repositories_read=tuple(done),
            read_at=now,
            calls=self._calls,
        )
