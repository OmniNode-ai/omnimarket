# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The attempt a re-run started (OMN-19831, contract 1.1.0, plan revision 1 F7).

``rerun_runs`` reads each run back after its rerun-failed-jobs call, so the
orchestrator can record the attempt as the check's ``expected_attempt``: a
later result older than it counts as pending, not as a second failure.
``run_attempt`` is None only when that read-back got no usable answer; the
re-run itself was accepted either way.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelGithubRunAttempt(BaseModel):
    """One workflow run and the attempt its re-run started."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: int = Field(gt=0)
    run_attempt: int | None = Field(ge=1)


__all__: list[str] = ["ModelGithubRunAttempt"]
