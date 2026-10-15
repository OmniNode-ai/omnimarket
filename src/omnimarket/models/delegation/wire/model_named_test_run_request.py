# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a delegation names to test a code answer, and the run that tests it (OMN-21077).

A caller names the tests in the delegate request's ``metadata`` map, which every
released request consumer already accepts, so naming tests changes no wire
shape. ``ModelNamedTestSpec.from_metadata`` reads them back into a typed spec;
``ModelNamedTestRunRequest`` is that spec plus the answer, the input of the
named-test runner the in-process delegation port is given, whose
``ModelNamedTestRun`` the quality gate's ``passes_existing_tests`` check reads.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.delegation.wire.model_named_test_run import ModelNamedTestRun

#: The pytest node ids to run, separated by whitespace.
NAMED_TESTS_METADATA_KEY = "named_tests"
#: Where in the workspace the answer's code is written before the tests run.
NAMED_TESTS_ANSWER_PATH_METADATA_KEY = "named_tests_answer_path"
#: The workspace the tests run in. A copy of it, never the directory itself.
NAMED_TESTS_WORKSPACE_METADATA_KEY = "named_tests_workspace"
#: The test files themselves, as a JSON object of workspace-relative path to
#: source (OMN-17427). A request that carries its tests needs no workspace, so
#: the runner can test the answer wherever the delegation runs.
NAMED_TESTS_FILES_METADATA_KEY = "named_tests_files"

MAX_NAMED_TESTS = 20
MAX_NAMED_TEST_FILES = 20
MAX_NAMED_TEST_FILE_CHARS = 200_000
DEFAULT_NAMED_TEST_TIMEOUT_SECONDS = 120


def _inside_workspace(relative: str) -> bool:
    path = PurePosixPath(relative)
    return not (path.is_absolute() or ".." in path.parts or ".git" in path.parts)


class ModelNamedTestFile(BaseModel):
    """One test file a request carries: where it goes in the sandbox, and its source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(..., min_length=1, description="Workspace-relative path.")
    content: str = Field(..., max_length=MAX_NAMED_TEST_FILE_CHARS)

    @model_validator(mode="after")
    def _validate_path(self) -> Self:
        if not _inside_workspace(self.path):
            raise ValueError(f"test file {self.path!r} must stay inside the workspace")
        return self


class ModelNamedTestSpec(BaseModel):
    """The tests a delegation names, the workspace they run in, and the answer file.

    ``workspace_root`` is copied into the sandbox when set; ``test_files`` are
    written into it after the copy. At least one of the two must be given.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_root: str | None = Field(
        default=None,
        min_length=1,
        description="Absolute path of the workspace to copy.",
    )
    test_files: tuple[ModelNamedTestFile, ...] = Field(
        default=(),
        max_length=MAX_NAMED_TEST_FILES,
        description="Test files the request carries, written into the sandbox.",
    )
    answer_path: str = Field(
        ...,
        min_length=1,
        description="Workspace-relative file the answer's code is written to.",
    )
    test_ids: tuple[str, ...] = Field(
        ...,
        min_length=1,
        max_length=MAX_NAMED_TESTS,
        description="Workspace-relative pytest node ids, run one at a time.",
    )

    @model_validator(mode="after")
    def _validate_paths(self) -> Self:
        if self.workspace_root is None and not self.test_files:
            raise ValueError("named tests need a workspace_root or test_files")
        if (
            self.workspace_root is not None
            and not PurePosixPath(self.workspace_root).is_absolute()
        ):
            raise ValueError("workspace_root must be an absolute path")
        if not _inside_workspace(self.answer_path):
            raise ValueError("answer_path must stay inside the workspace")
        if any(f.path == self.answer_path for f in self.test_files):
            raise ValueError("a test file cannot be written over answer_path")
        for test_id in self.test_ids:
            path = PurePosixPath(test_id.split("::", 1)[0])
            if not test_id.strip() or test_id.startswith("-"):
                raise ValueError(f"test id {test_id!r} is not a pytest node id")
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"test id {test_id!r} must stay inside the workspace")
        return self

    @classmethod
    def from_metadata(
        cls, metadata: Mapping[str, str], *, default_workspace: str | None = None
    ) -> ModelNamedTestSpec | None:
        """Read the named tests from a request's metadata; ``None`` when none are named.

        Raises ``ValueError`` when tests are named but the spec is incomplete or
        unsafe, so a caller who named tests is told rather than silently ignored.
        """
        raw_tests = metadata.get(NAMED_TESTS_METADATA_KEY, "").split()
        if not raw_tests:
            return None
        answer_path = metadata.get(NAMED_TESTS_ANSWER_PATH_METADATA_KEY)
        if not answer_path:
            raise ValueError(
                f"{NAMED_TESTS_METADATA_KEY} names tests but "
                f"{NAMED_TESTS_ANSWER_PATH_METADATA_KEY} is not set"
            )
        test_files = _test_files_from_metadata(
            metadata.get(NAMED_TESTS_FILES_METADATA_KEY, "")
        )
        workspace = metadata.get(NAMED_TESTS_WORKSPACE_METADATA_KEY) or (
            None if test_files else default_workspace
        )
        if not workspace and not test_files:
            raise ValueError(
                f"{NAMED_TESTS_METADATA_KEY} names tests but neither a workspace "
                f"({NAMED_TESTS_WORKSPACE_METADATA_KEY} or the request's working "
                f"directory) nor {NAMED_TESTS_FILES_METADATA_KEY} is set"
            )
        return cls(
            workspace_root=workspace or None,
            test_files=test_files,
            answer_path=answer_path,
            test_ids=tuple(raw_tests),
        )


def _test_files_from_metadata(raw: str) -> tuple[ModelNamedTestFile, ...]:
    """Parse ``named_tests_files``: a JSON object of path to source."""
    if not raw.strip():
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{NAMED_TESTS_FILES_METADATA_KEY} is not JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        raise ValueError(
            f"{NAMED_TESTS_FILES_METADATA_KEY} must be a JSON object of path to source"
        )
    return tuple(
        ModelNamedTestFile(path=path, content=content)
        for path, content in sorted(parsed.items())
    )


class ModelNamedTestRunRequest(BaseModel):
    """Input of a named-test runner: the named tests and the answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    spec: ModelNamedTestSpec
    answer_content: str = Field(
        ..., description="The delegated answer; its code is written to answer_path."
    )
    timeout_seconds: int = Field(
        default=DEFAULT_NAMED_TEST_TIMEOUT_SECONDS,
        ge=1,
        le=900,
        description="Per named test.",
    )


class EnumNamedTestRunStatus(StrEnum):
    """Whether the named tests ran; ``completed`` holds even when tests failed."""

    COMPLETED = "completed"
    REFUSED = "refused"


class ModelNamedTestRunResult(BaseModel):
    """Output of a named-test runner.

    ``run`` is present only when the tests ran. A refused run (an unusable
    workspace, an answer path that escapes it) carries no run, so the gate
    treats it as no tests named: never a pass.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumNamedTestRunStatus
    run: ModelNamedTestRun | None = None
    detail: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def _run_matches_status(self) -> Self:
        if (self.status is EnumNamedTestRunStatus.COMPLETED) != (self.run is not None):
            raise ValueError("a completed result carries a run; a refused one none")
        return self


__all__ = [
    "DEFAULT_NAMED_TEST_TIMEOUT_SECONDS",
    "MAX_NAMED_TESTS",
    "MAX_NAMED_TEST_FILES",
    "MAX_NAMED_TEST_FILE_CHARS",
    "NAMED_TESTS_ANSWER_PATH_METADATA_KEY",
    "NAMED_TESTS_FILES_METADATA_KEY",
    "NAMED_TESTS_METADATA_KEY",
    "NAMED_TESTS_WORKSPACE_METADATA_KEY",
    "EnumNamedTestRunStatus",
    "ModelNamedTestFile",
    "ModelNamedTestRunRequest",
    "ModelNamedTestRunResult",
    "ModelNamedTestSpec",
]
