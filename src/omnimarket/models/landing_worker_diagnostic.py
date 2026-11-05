# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Worker-reported diagnostics carried with a landing outcome event.

These fields record what the result file said. They do not verify the
worker's explanation, authorize an action, or replace the controller verdict.
"""

from pydantic import BaseModel, ConfigDict


class ModelLandingWorkerDiagnostic(BaseModel):
    """The explanatory fields of the result consumed for this lease."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    reason: str | None = None
    blocker_kind: str | None = None
    blocker_ref: str | None = None
