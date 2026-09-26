# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelFocusedTestRunRequest — one focused pytest run in a throwaway container
(OMN-19359, task T3 of the delegated test loop first slice).

The third operation on ``node_push_validation_effect`` (``run_focused_test_run``).
It runs ONE pytest node id against ONE exact commit of ONE public repository,
with caller-supplied overlay files written into a fresh per-task worktree, and
returns a receipt. It never pushes, never installs hooks and never touches a
branch — the same read-only posture as ``run_suite_evaluation``, with three
differences that make it a separate operation rather than a variant of that
one: the checkout is per TASK (keyed by correlation id, ref role and attempt,
so two runs never move each other's HEAD), the test file is an uncommitted
overlay, and the run happens in a new container that mounts only that
worktree and has no network.

Every path the caller names is RELATIVE to the repository root and may not
escape it. The container side is fixed by the invocation builder
(``protocols/dtl_container_invocation.py``); nothing on this request can add a
mount, a network, a capability or an environment variable.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.nodes.node_push_validation_effect.protocols.dtl_container_invocation import (
    check_gate_path,
)

#: The plan's bound on a single run (``timeout_seconds`` at most 600).
MAX_TIMEOUT_SECONDS: int = 600
#: Overlay content cap per file; a generated test is a few KB.
MAX_OVERLAY_BYTES: int = 262_144

_NODE_ID_PATTERN = re.compile(
    r"^tests/[A-Za-z0-9_./-]+\.py(::[A-Za-z0-9_\[\]().,=-]+)*$"
)


def check_relative_repo_path(value: str) -> str:
    """Refuse an absolute path, a parent reference or an empty component."""
    if not value or value != value.strip():
        raise ValueError(f"path is empty or padded: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or value.startswith("~"):
        raise ValueError(f"path must be relative to the repository root: {value!r}")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError(f"path may not contain '.', '..' or empty parts: {value!r}")
    if value.startswith(".git/") or value == ".git" or "/.git/" in value:
        raise ValueError(f"path may not reach into git metadata: {value!r}")
    return value


class ModelSourceMutation(BaseModel):
    """One exact find-and-replace applied to a file in the task worktree.

    The ruling-(3) mutation control (operator ruling 2026-09-23T21:52:08Z): when
    the pre-fix code lacks the symbol a test imports, the headline control runs
    the test against the FIXED commit with the behaviour mutated. ``find`` must
    occur exactly once in the file, or the run is refused as an infrastructure
    error before any container starts.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(..., max_length=512)
    find: str = Field(..., min_length=1, max_length=20_000)
    replace: str = Field(..., max_length=20_000)

    @field_validator("path")
    @classmethod
    def _path_is_relative(cls, value: str) -> str:
        return check_relative_repo_path(value)


class ModelFocusedTestRunRequest(BaseModel):
    """Run one pytest node id at one commit in a per-task container."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(
        ...,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
        max_length=140,
        description="Public repository slug (owner/name). Fetched anonymously.",
    )
    commit_sha: str = Field(
        ...,
        pattern=r"^[0-9a-f]{40}$",
        description="The exact commit checked out detached in the task worktree.",
    )
    overlay_files: dict[str, str] = Field(
        default_factory=dict,
        description="Relative path -> file content, written after checkout.",
    )
    hide_paths: tuple[str, ...] = Field(
        default=(),
        description="Relative paths deleted from the worktree after checkout "
        "(the hidden human test, so the model's test is judged alone).",
    )
    mutations: tuple[ModelSourceMutation, ...] = Field(
        default=(),
        description="Exact find/replace edits applied after the overlay.",
    )
    test_node_id: str = Field(
        ...,
        max_length=512,
        description="The pytest node id to run, relative to the repository root.",
    )
    timeout_seconds: int = Field(..., ge=1, le=MAX_TIMEOUT_SECONDS)
    correlation_id: str = Field(..., description="UUID of the loop run.")
    ref_role: Literal["fixed", "prefix", "mutation"] = Field(
        ...,
        description="Which control this run is: the fixed ref, the pre-fix "
        "ref, or the mutated fixed ref.",
    )
    attempt: int = Field(..., ge=1, le=9)
    gate_paths: tuple[str, ...] = Field(
        default=(),
        max_length=4,
        description="OMN-19527: repository-relative .py files the repository's "
        "lint and type gates (ruff check, ruff format --check, mypy --strict) "
        "run over in the same container after the test; each gate's exit code "
        "and output come back on the receipt. Empty runs no gate.",
    )

    @field_validator("gate_paths")
    @classmethod
    def _gate_paths_are_repo_files(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for path in value:
            check_relative_repo_path(path)
            check_gate_path(path)
        return value

    @field_validator("correlation_id")
    @classmethod
    def _correlation_id_is_uuid(cls, value: str) -> str:
        UUID(value)
        return value

    @field_validator("test_node_id")
    @classmethod
    def _node_id_is_a_repo_test(cls, value: str) -> str:
        if ".." in value or not _NODE_ID_PATTERN.fullmatch(value):
            raise ValueError(
                f"test_node_id must be a tests/... node id with no '..': {value!r}"
            )
        return value

    @field_validator("hide_paths")
    @classmethod
    def _hide_paths_are_relative(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for path in value:
            check_relative_repo_path(path)
        return value

    @field_validator("overlay_files")
    @classmethod
    def _overlay_is_bounded_and_relative(cls, value: dict[str, str]) -> dict[str, str]:
        for path, content in value.items():
            check_relative_repo_path(path)
            if len(content.encode("utf-8")) > MAX_OVERLAY_BYTES:
                raise ValueError(
                    f"overlay file exceeds {MAX_OVERLAY_BYTES} bytes: {path}"
                )
        return value

    @model_validator(mode="after")
    def _test_file_is_not_hidden(self) -> ModelFocusedTestRunRequest:
        test_file = self.test_node_id.split("::", 1)[0]
        if test_file in self.hide_paths:
            raise ValueError("test_node_id names a file this request hides")
        return self


__all__ = [
    "MAX_OVERLAY_BYTES",
    "MAX_TIMEOUT_SECONDS",
    "ModelFocusedTestRunRequest",
    "ModelSourceMutation",
    "check_relative_repo_path",
]
