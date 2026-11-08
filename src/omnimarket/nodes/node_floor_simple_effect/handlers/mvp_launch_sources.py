# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the MVP launch block's run histories and candidate manifest come from (OMN-23205, plan task M1).

A port with two backends: an in-memory one (the default for tests and a dry run) and a command route, in which the
command named by the overlay's environment variable reads one JSON request on stdin and answers one JSON document on
stdout. A command that is not configured, fails, times out or answers something unreadable is an unreadable source:
the reading is NOT WORKING with the reason, never skipped and never WORKING.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Mapping
from typing import Protocol

from pydantic import ValidationError

from omnimarket.models.floor_17a.model_mvp_launch import (
    ModelMvpCandidate,
    ModelMvpReadingSpec,
    ModelMvpRun,
    ModelMvpSourceHistory,
)


class ProtocolMvpRunSource(Protocol):
    def read_history(self, spec: ModelMvpReadingSpec) -> ModelMvpSourceHistory: ...

    def read_candidate(self) -> ModelMvpCandidate: ...


class InMemoryMvpRunSource:
    """Recorded histories keyed by reading id; an id with no history is unreadable."""

    def __init__(
        self,
        histories: Mapping[str, ModelMvpSourceHistory] | None = None,
        candidate: ModelMvpCandidate | None = None,
    ) -> None:
        self._histories = dict(histories or {})
        self._candidate = candidate or ModelMvpCandidate(error="no candidate supplied")

    def read_history(self, spec: ModelMvpReadingSpec) -> ModelMvpSourceHistory:
        return self._histories.get(
            spec.id, ModelMvpSourceHistory(error="no history supplied")
        )

    def read_candidate(self) -> ModelMvpCandidate:
        return self._candidate


class CommandMvpRunSource:
    """Reads through the configured command: ``{"query": "runs"|"candidate", ...}`` in, one JSON answer out."""

    def __init__(self, command: str, timeout_seconds: int) -> None:
        self._argv = shlex.split(command)
        self._timeout = timeout_seconds

    def _ask(self, request: dict[str, object]) -> dict[str, object] | str:
        """The answer object, or the reason it could not be had."""
        if not self._argv:
            return "read-command-not-configured"
        try:
            done = subprocess.run(
                self._argv,
                input=json.dumps(request),
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"read-command-failed:{type(exc).__name__}"
        if done.returncode != 0:
            return f"read-command-exit-{done.returncode}"
        try:
            answer = json.loads(done.stdout)
        except ValueError:
            return "read-command-answer-not-json"
        if not isinstance(answer, dict):
            return "read-command-answer-not-an-object"
        return answer

    def read_history(self, spec: ModelMvpReadingSpec) -> ModelMvpSourceHistory:
        if spec.source is None:
            return ModelMvpSourceHistory(error="reading names no source")
        answer = self._ask(
            {
                "query": "runs",
                "repo": spec.source.repo,
                "workflow": spec.source.workflow,
                "limit": spec.source.limit,
                "triggers": ["schedule", *spec.admitted_triggers],
            }
        )
        if isinstance(answer, str):
            return ModelMvpSourceHistory(error=answer)
        try:
            runs = tuple(ModelMvpRun.model_validate(r) for r in answer.get("runs", ()))  # type: ignore[attr-defined]
        except (ValidationError, TypeError, AttributeError):
            return ModelMvpSourceHistory(error="read-command-runs-malformed")
        return ModelMvpSourceHistory(runs=runs)

    def read_candidate(self) -> ModelMvpCandidate:
        answer = self._ask({"query": "candidate"})
        if isinstance(answer, str):
            return ModelMvpCandidate(error=answer)
        versions = answer.get("versions")
        if not isinstance(versions, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in versions.items()
        ):
            return ModelMvpCandidate(error="candidate-manifest-malformed")
        return ModelMvpCandidate(versions=versions)
