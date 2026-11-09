# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The GitHub check-run API: list a head's runs of one name, create a run, update a run.

The token is an App installation token (the check-run API refuses any other); ``token_env`` names the environment
variable that holds it, and neither the token nor a response body reaches an error message. The attempt identity is the
run's ``external_id``. Calls are bounded by the overlay's ``max_calls``: the call past it is not made.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from omnimarket.nodes.node_lab_check_run_effect.models.model_lab_check_run import (
    ModelCheckRunRecord,
)
from omnimarket.nodes.node_lab_check_run_effect.overlay import ModelGithubSource
from omnimarket.nodes.node_lab_check_run_effect.protocols.protocol_check_run_api import (
    CheckRunApiError,
    ProtocolHttpJson,
)


class GithubCheckRunApi:
    def __init__(
        self,
        source: ModelGithubSource,
        http: ProtocolHttpJson,
        environ: Mapping[str, str],
    ) -> None:
        self._source = source
        self._http = http
        self._environ = environ
        self._calls = 0

    @property
    def calls(self) -> int:
        return self._calls

    def _call(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        token = self._environ.get(self._source.token_env, "")
        if not token:
            raise CheckRunApiError(
                f"environment variable {self._source.token_env} is not set"
            )
        if self._calls >= self._source.max_calls:
            raise CheckRunApiError(f"over the {self._source.max_calls} call budget")
        self._calls += 1
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self._source.api_version,
            "Content-Type": "application/json",
        }
        payload = None if body is None else json.dumps(body).encode("utf-8")
        status, raw = self._http.request(
            method,
            f"{self._source.api_base}{path}",
            headers,
            payload,
            self._source.timeout_seconds,
        )
        if status >= 400:
            raise CheckRunApiError(f"{method} {path} answered {status}")
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise CheckRunApiError(f"{method} {path} answered unreadable JSON") from exc
        if not isinstance(parsed, dict):
            raise CheckRunApiError(f"{method} {path} answered a non-object")
        return parsed

    @staticmethod
    def _record(repository: str, row: Mapping[str, Any]) -> ModelCheckRunRecord:
        try:
            return ModelCheckRunRecord(
                check_run_id=row["id"],
                repository=repository,
                name=row["name"],
                head_sha=row["head_sha"],
                attempt_id=row.get("external_id") or "",
                status=row["status"],
                conclusion=row.get("conclusion"),
            )
        except (KeyError, ValueError) as exc:
            raise CheckRunApiError(
                "a check run did not have the expected shape"
            ) from exc

    def list_runs(
        self, *, repository: str, head_sha: str, check_name: str
    ) -> tuple[ModelCheckRunRecord, ...]:
        records: list[ModelCheckRunRecord] = []
        page = 1
        while True:
            body = self._call(
                "GET",
                f"/repos/{repository}/commits/{head_sha}/check-runs"
                f"?filter=all&per_page={self._source.page_size}&page={page}",
            )
            rows = body.get("check_runs")
            if not isinstance(rows, list):
                raise CheckRunApiError("the check-run list had no check_runs array")
            records.extend(self._record(repository, row) for row in rows)
            if len(rows) < self._source.page_size:
                break
            page += 1
        return tuple(r for r in records if r.name == check_name)

    def create_run(
        self,
        *,
        repository: str,
        head_sha: str,
        name: str,
        attempt_id: str,
        status: str,
        conclusion: str | None,
        title: str,
        summary: str,
    ) -> ModelCheckRunRecord:
        body: dict[str, Any] = {
            "name": name,
            "head_sha": head_sha,
            "external_id": attempt_id,
            "status": status,
            "output": {"title": title, "summary": summary},
        }
        if conclusion is not None:
            body["conclusion"] = conclusion
        return self._record(
            repository, self._call("POST", f"/repos/{repository}/check-runs", body)
        )

    def update_run(
        self,
        *,
        repository: str,
        check_run_id: int,
        status: str,
        conclusion: str | None,
        title: str,
        summary: str,
    ) -> ModelCheckRunRecord:
        body: dict[str, Any] = {
            "status": status,
            "output": {"title": title, "summary": summary},
        }
        if conclusion is not None:
            body["conclusion"] = conclusion
        return self._record(
            repository,
            self._call("PATCH", f"/repos/{repository}/check-runs/{check_run_id}", body),
        )
