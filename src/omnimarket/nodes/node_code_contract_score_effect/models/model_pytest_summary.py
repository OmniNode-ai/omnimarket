# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The counts of pytest's closing summary line (OMN-23637)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelPytestSummary(BaseModel):
    """What pytest's last line (``1 failed, 2 passed, 1 error in 0.40s``) says about a run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    failed: int = Field(default=0, ge=0, description="Tests that failed.")
    errors: int = Field(
        default=0, ge=0, description="Errors at setup, teardown or collection."
    )
    passed: int = Field(default=0, ge=0, description="Tests that passed.")

    @property
    def ended_in_error_with_no_failed_test(self) -> bool:
        """True when the run reports an error and no failed test."""
        return self.errors > 0 and self.failed == 0


__all__ = ["ModelPytestSummary"]
