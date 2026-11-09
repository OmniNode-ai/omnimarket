# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the MVP launch block's run histories and candidate manifest come from (OMN-23205, plan task M1).

A port with three backends: an in-memory one (for tests and a dry run), the record the publish's scheduled read keeps (the deployed
default) and a command route, in which the command named by the overlay's environment variable reads one JSON request on stdin and
answers one JSON document on stdout. An answer may be ``{"error": reason}``: the source is unreadable for that reason. A command that is not configured, fails,
times out or answers something unreadable is an unreadable source:
the reading is NOT WORKING with the reason, never skipped and never WORKING.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from omnimarket.models.floor_17a.model_mvp_launch import (
    ModelMvpCandidate,
    ModelMvpReadingSpec,
    ModelMvpRun,
    ModelMvpSourceHistory,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.mvp_launch_reads import (
    answer,
    read_candidate_manifest,
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


class RecordMvpRunSource:
    """Reads the record that the publish's scheduled read keeps and the launch candidate manifest file, in this process.

    The deployed backend: the cycle asks for each of the block's two dozen histories, and a command route would start a process for
    each inside a step that has 15 seconds. A workflow the record does not hold, a read older than its bound, a missing record and a
    missing or malformed candidate manifest are each an unreadable source with that reason.
    """

    def __init__(
        self,
        reads_path: Path,
        candidate_path: Path,
        reads_max_age_seconds: int,
        now: datetime,
    ) -> None:
        self._reads_path = reads_path
        self._candidate_path = candidate_path
        self._max_age = reads_max_age_seconds
        self._now = now

    def read_history(self, spec: ModelMvpReadingSpec) -> ModelMvpSourceHistory:
        if spec.source is None:
            return ModelMvpSourceHistory(error="reading names no source")
        got = answer(
            {
                "query": "runs",
                "repo": spec.source.repo,
                "workflow": spec.source.workflow,
                "triggers": ["schedule", *spec.admitted_triggers],
            },
            reads_path=self._reads_path,
            candidate_path=self._candidate_path,
            reads_max_age_seconds=self._max_age,
            now=self._now,
        )
        error = got.get("error")
        if isinstance(error, str):
            return ModelMvpSourceHistory(error=error)
        raw_runs = got.get("runs", [])
        if not isinstance(raw_runs, list):
            return ModelMvpSourceHistory(error="record-runs-malformed")
        try:
            return ModelMvpSourceHistory(
                runs=tuple(ModelMvpRun.model_validate(r) for r in raw_runs)
            )
        except ValidationError:
            return ModelMvpSourceHistory(error="record-runs-malformed")

    def read_candidate(self) -> ModelMvpCandidate:
        got = read_candidate_manifest(self._candidate_path)
        error = got.get("error")
        if isinstance(error, str):
            return ModelMvpCandidate(error=error)
        versions = got.get("versions")
        if not isinstance(versions, dict):
            return ModelMvpCandidate(error="candidate-manifest-malformed")
        return ModelMvpCandidate(versions={str(k): str(v) for k, v in versions.items()})


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
        if isinstance(answer.get("error"), str):
            return ModelMvpSourceHistory(error=str(answer["error"]))
        raw_runs = answer.get("runs", [])
        if not isinstance(raw_runs, list):
            return ModelMvpSourceHistory(error="read-command-runs-malformed")
        try:
            runs = tuple(ModelMvpRun.model_validate(r) for r in raw_runs)
        except ValidationError:
            return ModelMvpSourceHistory(error="read-command-runs-malformed")
        return ModelMvpSourceHistory(runs=runs)

    def read_candidate(self) -> ModelMvpCandidate:
        answer = self._ask({"query": "candidate"})
        if isinstance(answer, str):
            return ModelMvpCandidate(error=answer)
        if isinstance(answer.get("error"), str):
            return ModelMvpCandidate(error=str(answer["error"]))
        versions = answer.get("versions")
        if not isinstance(versions, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in versions.items()
        ):
            return ModelMvpCandidate(error="candidate-manifest-malformed")
        return ModelMvpCandidate(versions=versions)
