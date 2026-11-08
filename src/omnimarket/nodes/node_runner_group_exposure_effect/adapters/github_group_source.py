# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real group source: reads one runner group through the GitHub REST API.

Every call is counted against the overlay's ``max_calls``; the call past it is not made and the read
ends in ``budget-exhausted``. Reads only (GET). The token comes from the environment variable the
overlay names, at call time, and is never put in a result, an error or a log line.

Reading rules, all conservative so that an unreadable fact reads UNKNOWN and never WORKING:
- a job selects the group when its ``runs-on`` (string, list or ``labels``) names a group label, or
  holds an expression (``${{ ... }}``) the probe cannot resolve, or calls a reusable workflow that does;
- a reusable workflow that cannot be read ends the read in ``unreadable``.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import yaml

from omnimarket.models.runner_group_exposure import (
    EnumExposureReadFailure,
    ModelGroupJob,
    ModelGroupWorkflowFile,
    ModelRunnerGroupFacts,
)
from omnimarket.nodes.node_runner_group_exposure_effect.models.model_exposure_probe import (
    ModelGroupRead,
)
from omnimarket.nodes.node_runner_group_exposure_effect.overlay import (
    ModelGithubSource,
)
from omnimarket.nodes.node_runner_group_exposure_effect.protocols.protocol_group_source import (
    ProtocolHttpGet,
)

_DENIED = (401, 403, 404)
_RUN_STATUSES = ("queued", "in_progress")


class _StopError(Exception):
    def __init__(self, failure: EnumExposureReadFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure


class GithubGroupSource:
    def __init__(
        self,
        settings: ModelGithubSource,
        http_get: ProtocolHttpGet,
        environ: Mapping[str, str],
    ) -> None:
        self._s = settings
        self._http = http_get
        self._environ = environ
        self._calls = 0

    # -- transport -----------------------------------------------------------------------
    def _get(self, path: str, *, denied_is_org: bool = False) -> Any:
        if self._calls >= self._s.max_calls:
            raise _StopError(EnumExposureReadFailure.BUDGET_EXHAUSTED)
        token = self._environ.get(self._s.token_env, "")
        if not token:
            raise _StopError(EnumExposureReadFailure.ORG_READ_DENIED)
        self._calls += 1
        status, body = self._http.get(
            f"{self._s.api_base.rstrip('/')}{path}",
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            self._s.timeout_seconds,
        )
        if status in _DENIED and denied_is_org:
            raise _StopError(EnumExposureReadFailure.ORG_READ_DENIED)
        if status != 200:
            raise _StopError(EnumExposureReadFailure.UNREADABLE)
        try:
            return json.loads(body)
        except ValueError as error:
            raise _StopError(EnumExposureReadFailure.UNREADABLE) from error

    def _optional(self, path: str) -> Any | None:
        """A GET whose 404 means the thing is absent (a repository with no workflows)."""
        if self._calls >= self._s.max_calls:
            raise _StopError(EnumExposureReadFailure.BUDGET_EXHAUSTED)
        token = self._environ.get(self._s.token_env, "")
        if not token:
            raise _StopError(EnumExposureReadFailure.ORG_READ_DENIED)
        self._calls += 1
        status, body = self._http.get(
            f"{self._s.api_base.rstrip('/')}{path}",
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            self._s.timeout_seconds,
        )
        if status == 404:
            return None
        if status != 200:
            raise _StopError(EnumExposureReadFailure.UNREADABLE)
        try:
            return json.loads(body)
        except ValueError as error:
            raise _StopError(EnumExposureReadFailure.UNREADABLE) from error

    # -- parsing -------------------------------------------------------------------------
    def _document(self, repository: str, path: str, ref: str | None) -> dict[str, Any]:
        query = f"?ref={ref}" if ref else ""
        meta = self._get(f"/repos/{repository}/contents/{path}{query}")
        try:
            text = base64.b64decode(meta["content"]).decode("utf-8")
            loaded = yaml.safe_load(text)
        except (
            KeyError,
            TypeError,
            ValueError,
            binascii.Error,
            yaml.YAMLError,
        ) as error:
            raise _StopError(EnumExposureReadFailure.UNREADABLE) from error
        if not isinstance(loaded, dict):
            raise _StopError(EnumExposureReadFailure.UNREADABLE)
        return loaded

    @staticmethod
    def _triggers(document: Mapping[Any, Any]) -> tuple[str, ...]:
        # YAML 1.1 reads the key ``on`` as the boolean True.
        raw = document.get("on", document.get(True))
        if isinstance(raw, str):
            return (raw,)
        if isinstance(raw, list):
            return tuple(str(item) for item in raw)
        if isinstance(raw, dict):
            return tuple(str(key) for key in raw)
        return ()

    def _runs_on_selects(self, runs_on: object) -> bool:
        labels = set(self._s.group_labels)
        if isinstance(runs_on, str):
            return "${{" in runs_on or runs_on in labels
        if isinstance(runs_on, list):
            return any(self._runs_on_selects(item) for item in runs_on)
        if isinstance(runs_on, dict):
            return any(
                self._runs_on_selects(runs_on[key])
                for key in ("labels", "group")
                if key in runs_on
            )
        return False

    def _selects(
        self, repository: str, document: Mapping[Any, Any], depth: int = 0
    ) -> bool:
        jobs = document.get("jobs")
        if not isinstance(jobs, dict):
            return False
        for job in jobs.values():
            if not isinstance(job, dict):
                continue
            if self._runs_on_selects(job.get("runs-on")):
                return True
            uses = job.get("uses")
            if (
                isinstance(uses, str)
                and depth < 3
                and self._uses_selects(repository, uses, depth)
            ):
                return True
        return False

    def _uses_selects(self, repository: str, uses: str, depth: int) -> bool:
        target, _, ref = uses.partition("@")
        if target.startswith("./"):
            document = self._document(repository, target[2:], None)
            return self._selects(repository, document, depth + 1)
        parts = target.split("/", 2)
        if len(parts) != 3 or not ref:
            raise _StopError(EnumExposureReadFailure.UNREADABLE)
        document = self._document(f"{parts[0]}/{parts[1]}", parts[2], ref)
        return self._selects(f"{parts[0]}/{parts[1]}", document, depth + 1)

    # -- the read ------------------------------------------------------------------------
    def read(self, *, now: datetime) -> ModelGroupRead:
        try:
            group, repositories = self._group()
            files = self._files(repositories)
            jobs = self._jobs(repositories, files)
        except _StopError as stop:
            return ModelGroupRead(
                read_at=now, calls=self._calls, read_failure=stop.failure
            )
        return ModelGroupRead(
            group=group, files=files, jobs=jobs, read_at=now, calls=self._calls
        )

    def _group(self) -> tuple[ModelRunnerGroupFacts, tuple[str, ...]]:
        base = f"/orgs/{self._s.org}/actions/runner-groups/{self._s.group_id}"
        meta = self._get(base, denied_is_org=True)
        listing = self._get(f"{base}/repositories", denied_is_org=True)
        try:
            repositories = tuple(str(r["full_name"]) for r in listing["repositories"])
            facts = ModelRunnerGroupFacts(
                group_id=self._s.group_id,
                allows_public_repositories=bool(meta["allows_public_repositories"]),
                restricted_to_workflows=bool(meta["restricted_to_selected_workflows"]),
                selected_workflows=tuple(
                    str(w) for w in meta.get("selected_workflows") or ()
                ),
                repositories=repositories,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise _StopError(EnumExposureReadFailure.UNREADABLE) from error
        return facts, repositories

    def _files(
        self, repositories: tuple[str, ...]
    ) -> tuple[ModelGroupWorkflowFile, ...]:
        files: list[ModelGroupWorkflowFile] = []
        for repository in repositories:
            branch = self._get(f"/repos/{repository}")
            try:
                ref = str(branch["default_branch"])
            except (KeyError, TypeError) as error:
                raise _StopError(EnumExposureReadFailure.UNREADABLE) from error
            listing = self._optional(
                f"/repos/{repository}/contents/.github/workflows?ref={ref}"
            )
            if listing is None:
                continue
            if not isinstance(listing, list):
                raise _StopError(EnumExposureReadFailure.UNREADABLE)
            for entry in listing:
                try:
                    path = str(entry["path"])
                    kind = str(entry["type"])
                except (KeyError, TypeError) as error:
                    raise _StopError(EnumExposureReadFailure.UNREADABLE) from error
                if kind != "file" or not path.endswith((".yml", ".yaml")):
                    continue
                document = self._document(repository, path, ref)
                files.append(
                    ModelGroupWorkflowFile(
                        repository=repository,
                        path=path,
                        ref=ref,
                        selects_group_label=self._selects(repository, document),
                        triggers=self._triggers(document),
                    )
                )
        return tuple(files)

    def _jobs(
        self,
        repositories: tuple[str, ...],
        files: tuple[ModelGroupWorkflowFile, ...],
    ) -> tuple[ModelGroupJob, ...]:
        selecting = {(f.repository, f.path) for f in files if f.selects_group_label}
        jobs: list[ModelGroupJob] = []
        for repository in repositories:
            for status in _RUN_STATUSES:
                listing = self._get(f"/repos/{repository}/actions/runs?status={status}")
                try:
                    runs = listing["workflow_runs"]
                    for run in runs:
                        path = str(run["path"]).split("@", 1)[0]
                        if (repository, path) not in selecting:
                            continue
                        head = (run.get("head_repository") or {}).get("full_name")
                        run_repository = run.get("repository") or {}
                        base = run_repository.get("full_name")
                        private = run_repository.get("private")
                        jobs.append(
                            ModelGroupJob(
                                run_id=int(run["id"]),
                                run_event=str(run["event"]),
                                head_is_fork=bool(head and base and head != base),
                                workflow_ref=path,
                                repository=str(base) if base else repository,
                                repository_private=(
                                    private if isinstance(private, bool) else None
                                ),
                            )
                        )
                except (KeyError, TypeError, ValueError) as error:
                    raise _StopError(EnumExposureReadFailure.UNREADABLE) from error
        return tuple(jobs)
