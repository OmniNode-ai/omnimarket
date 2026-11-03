# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reference function a test-class task supplies, and the run that uses it.

A ``test`` task asks for pytest tests of a function. When the task supplies the
function itself, ``ModelTestClassReferenceRunRequest.to_named_test_run_request``
writes the reference module beside the generated file and names the file
whole and then each generated test, so the existing named-test sandbox runner
(``HandlerNamedTestRun``) runs the generated tests against the reference. The
rubric's ``generated_tests_pass_reference`` criterion reads that run and
refuses a file whose assertions fail against the reference.
"""

from __future__ import annotations

import ast
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.delegation.graded_ladder.graders import extract_code_block
from omnimarket.models.delegation.wire.model_named_test_run_request import (
    DEFAULT_NAMED_TEST_TIMEOUT_SECONDS,
    MAX_NAMED_TEST_FILE_CHARS,
    MAX_NAMED_TESTS,
    ModelNamedTestFile,
    ModelNamedTestRunRequest,
    ModelNamedTestSpec,
)


def generated_test_ids(test_path: str, code: str) -> tuple[str, ...]:
    """Node ids of the module-level ``test*`` functions and ``Test*`` class methods."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ()
    ids: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name.startswith("test"):
                ids.append(f"{test_path}::{node.name}")
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            ids.extend(
                f"{test_path}::{node.name}::{item.name}"
                for item in node.body
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
                and item.name.startswith("test")
            )
    return tuple(dict.fromkeys(ids))


class ModelTestClassReference(BaseModel):
    """Where the reference module and the generated test file go, and the reference source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference_path: str = Field(
        ..., min_length=1, description="Workspace-relative module of the reference."
    )
    reference_source: str = Field(
        ..., min_length=1, max_length=MAX_NAMED_TEST_FILE_CHARS
    )
    test_path: str = Field(
        ...,
        min_length=1,
        description="Workspace-relative file the generated tests are written to.",
    )

    @model_validator(mode="after")
    def _validate_paths(self) -> Self:
        # Path containment is checked by the named-test file model.
        ModelNamedTestFile(path=self.reference_path, content="")
        ModelNamedTestFile(path=self.test_path, content="")
        if self.reference_path == self.test_path:
            raise ValueError("reference_path and test_path must differ")
        if not self.test_path.endswith(".py"):
            raise ValueError("test_path must be a Python file")
        return self


class ModelTestClassReferenceRunRequest(BaseModel):
    """Run one generated test file against the task's reference function."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference: ModelTestClassReference
    answer_content: str = Field(
        ..., description="The delegated answer; its code is the generated test file."
    )
    timeout_seconds: int = Field(
        default=DEFAULT_NAMED_TEST_TIMEOUT_SECONDS, ge=1, le=900
    )

    def to_named_test_run_request(self) -> ModelNamedTestRunRequest:
        """The named-test run of the generated file against the reference.

        The whole file comes first, so a collection error or a file with no
        test fails the run; then each generated test, so the evidence names
        each one that fails against the reference.
        """
        reference = self.reference
        test_ids = (
            reference.test_path,
            *generated_test_ids(
                reference.test_path, extract_code_block(self.answer_content)
            ),
        )[:MAX_NAMED_TESTS]
        return ModelNamedTestRunRequest(
            spec=ModelNamedTestSpec(
                answer_path=reference.test_path,
                test_ids=test_ids,
                test_files=(
                    ModelNamedTestFile(
                        path=reference.reference_path,
                        content=reference.reference_source,
                    ),
                ),
            ),
            answer_content=self.answer_content,
            timeout_seconds=self.timeout_seconds,
        )


__all__ = [
    "ModelTestClassReference",
    "ModelTestClassReferenceRunRequest",
    "generated_test_ids",
]
