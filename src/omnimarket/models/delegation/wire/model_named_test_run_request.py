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

MAX_NAMED_TESTS = 20
DEFAULT_NAMED_TEST_TIMEOUT_SECONDS = 120


class ModelNamedTestSpec(BaseModel):
    """The tests a delegation names, the workspace they run in, and the answer file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_root: str = Field(
        ..., min_length=1, description="Absolute path of the workspace to copy."
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
        if not PurePosixPath(self.workspace_root).is_absolute():
            raise ValueError("workspace_root must be an absolute path")
        answer = PurePosixPath(self.answer_path)
        if answer.is_absolute() or ".." in answer.parts or ".git" in answer.parts:
            raise ValueError("answer_path must stay inside the workspace")
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
        workspace = (
            metadata.get(NAMED_TESTS_WORKSPACE_METADATA_KEY) or default_workspace
        )
        if not workspace:
            raise ValueError(
                f"{NAMED_TESTS_METADATA_KEY} names tests but no workspace is set "
                f"({NAMED_TESTS_WORKSPACE_METADATA_KEY} or the request's "
                "working directory)"
            )
        return cls(
            workspace_root=workspace,
            answer_path=answer_path,
            test_ids=tuple(raw_tests),
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
    "NAMED_TESTS_ANSWER_PATH_METADATA_KEY",
    "NAMED_TESTS_METADATA_KEY",
    "NAMED_TESTS_WORKSPACE_METADATA_KEY",
    "EnumNamedTestRunStatus",
    "ModelNamedTestRunRequest",
    "ModelNamedTestRunResult",
    "ModelNamedTestSpec",
]
