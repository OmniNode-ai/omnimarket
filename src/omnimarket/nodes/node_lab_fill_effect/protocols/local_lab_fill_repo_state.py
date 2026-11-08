# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab fill's pull request read, through the repository state workflow (OMN-23033).

The lab fill reads a pull request's hold labels right before it launches a lane. It reads them
through ``onex repo-state query --host <h> --repo <owner/name> --want pull_request --pr <n>``
(topic ``onex.cmd.omnimarket.repo-state-query-requested.v1``): the host's mirror is compared with
the latest state the bus holds, the PR watcher state is refreshed first when it is behind, and the
answer comes from the mirror. The lab fill never reads the PR watcher's state file itself on a
host that declares the workflow, and makes no GitHub call.

The host, the repository owner and the timeout are deployment facts the host's install sets in
the environment variables named below; a host that sets none of them reads as before (the
watcher state file) until its install lands.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .protocol_lab_fill_effect import LabFillPortError

HOST_ENV = "ONEX_REPO_STATE_HOST"
OWNER_ENV = "ONEX_REPO_STATE_OWNER"
TIMEOUT_ENV = "ONEX_REPO_STATE_QUERY_TIMEOUT_SECONDS"
QUERY_ARGV: tuple[str, ...] = ("onex", "repo-state", "query")

Runner = Callable[..., Any]


class RepoStateCliReader:
    """Ask this host's mirror for one pull request's record."""

    def __init__(
        self,
        *,
        host: str,
        owner: str,
        timeout_seconds: float,
        argv: Sequence[str] = QUERY_ARGV,
        run: Runner = subprocess.run,
    ) -> None:
        self._host = host
        self._owner = owner
        self._timeout = timeout_seconds
        self._argv = tuple(argv)
        self._run = run

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> RepoStateCliReader | None:
        """The reader the host's install declares, or None when it declares none."""
        host, owner = env.get(HOST_ENV, "").strip(), env.get(OWNER_ENV, "").strip()
        raw = env.get(TIMEOUT_ENV, "").strip()
        if not (host and owner and raw):
            return None
        try:
            timeout = float(raw)
        except ValueError as exc:
            raise LabFillPortError(f"{TIMEOUT_ENV} is not a number") from exc
        if timeout <= 0:
            raise LabFillPortError(f"{TIMEOUT_ENV} must be positive")
        return cls(host=host, owner=owner, timeout_seconds=timeout)

    def pull_request_record(self, repo: str, number: str) -> Mapping[str, object]:
        argv = [
            *self._argv,
            "--host",
            self._host,
            "--repo",
            f"{self._owner}/{repo}",
            "--want",
            "pull_request",
            "--pr",
            number,
        ]
        try:
            done = self._run(
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise LabFillPortError(f"repo-state query: {type(exc).__name__}") from exc
        try:
            answer = json.loads(done.stdout)
        except (ValueError, TypeError) as exc:
            raise LabFillPortError(
                f"repo-state query: exit {done.returncode}, no JSON answer"
            ) from exc
        if done.returncode != 0 or not isinstance(answer, dict) or not answer.get("ok"):
            error = answer.get("error") if isinstance(answer, dict) else None
            raise LabFillPortError(
                f"repo-state query: {str(error or 'not ok')[:200]} (source {answer.get('source') if isinstance(answer, dict) else '?'})"
            )
        record = answer.get("record")
        if not isinstance(record, dict):
            raise LabFillPortError("repo-state query: the answer holds no record")
        return record


__all__ = [
    "HOST_ENV",
    "OWNER_ENV",
    "QUERY_ARGV",
    "TIMEOUT_ENV",
    "RepoStateCliReader",
]
