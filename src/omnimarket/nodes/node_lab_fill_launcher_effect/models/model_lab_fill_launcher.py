# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of one lab-fill launcher judgement.

The request is one fire as the tick observed it: how the run left it and what
its own written result file says. The result is the fire's receipt: the verdict
the tick records, the alert it delivered and the validated result it published.
``tick_line`` is the one JSON line the tick reads -- the seam with
``morning_workflows_tick.sh``; its key set is pinned by a test.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelLabFillLauncherRequest(BaseModel):
    """One fire as the tick observed it, before any judgement."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The fire's id, as the launcher named it.
    fire_id: str
    #: The phase the run left the fire in.
    phase: str
    #: The exit code the run left the fire with.
    exit: int
    #: A selftest fire is returned unjudged.
    selftest: bool = False
    #: The path named in the truncated notice ("" when none).
    workflow_result_file: str = ""
    #: Where the run log lives.
    run_log: str
    #: The fire had a workflow-result file named (the tick sets it for every
    #: non-selftest Workflow run).
    result_channel_enabled: bool


class ModelLabFillLauncherResult(BaseModel):
    """The fire's receipt: what to record, what was sent, what was published."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The phase to record, after the judgement.
    phase: str
    #: The exit code to record, after the judgement.
    exit: int
    #: The receipt's degraded class, when the fire is degraded.
    degraded_class: str | None
    #: How the fire's own written result file read (None only for a selftest).
    result_channel: str | None
    #: The completion notification was cut.
    result_truncated: bool
    #: The notice line to log ("" when none).
    notice_line: str
    #: The alert's delivery exit code (None when no alert was owed).
    alert_exit: int | None
    #: The alert's category value (None when no alert was owed).
    alert_category: str | None

    def tick_line(self) -> str:
        """The one JSON line the tick reads: the receipt's own fields."""
        return self.model_dump_json()


__all__: list[str] = [
    "ModelLabFillLauncherRequest",
    "ModelLabFillLauncherResult",
]
