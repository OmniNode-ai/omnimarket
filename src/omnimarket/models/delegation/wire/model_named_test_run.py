# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Outcome of running named tests against a delegated answer (OMN-21076).

Produced upstream by whatever executed the tests and read by the quality gate's
``passes_existing_tests`` check.
"""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# pytest exit 4 is a usage error (file or test id not found); exit 5 is no tests
# collected. Neither executed a test, so neither is evidence about the code. A
# run that carries one of them and names no failed test did not run its tests.
# This is read from the exit code, not a new field, because the released core
# wire model this is validated into (extra="forbid") carries only these three.
NOT_RUN_EXIT_CODES: frozenset[int] = frozenset({4, 5})


class ModelNamedTestRun(BaseModel):
    """Executed named-test evidence supplied to the pure quality gate reducer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    test_ids: tuple[str, ...] = Field(
        min_length=1,
        description="The named tests that ran against the delegated code.",
    )
    failed_test_ids: tuple[str, ...] = Field(
        default=(), description="The named tests that failed."
    )
    exit_code: int = Field(description="The test runner's exit code.")

    @model_validator(mode="after")
    def _validate_failed_test_ids(self) -> Self:
        if any(test_id not in self.test_ids for test_id in self.failed_test_ids):
            raise ValueError("failed_test_ids must contain only entries from test_ids")
        return self

    @property
    def not_run(self) -> bool:
        """Whether the named tests did not run (exit 4 or 5, none failed)."""
        return self.exit_code in NOT_RUN_EXIT_CODES and not self.failed_test_ids

    @property
    def passed(self) -> bool:
        """Whether the runner exited successfully with no named failures."""
        return self.exit_code == 0 and not self.failed_test_ids
