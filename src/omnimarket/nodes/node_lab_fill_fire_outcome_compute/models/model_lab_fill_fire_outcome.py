# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, alert and result of one lab-fill fire outcome judgement.

The request carries everything the tick observed about one fire -- how the run
left it, what its own written result file says, and what the run log showed --
plus the policy to judge under. The result is the fire's receipt: the phase,
exit and degraded class to record, the alert to deliver, and the validated
result to publish at the latest path. The judge performs no I/O: every fact it
needs has already been read.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_lab_fill_fire_outcome_compute.models.enum_lab_fill_fire_outcome import (
    EnumLabFillAlertCategory,
    EnumLabFillResultChannel,
)
from omnimarket.nodes.node_lab_fill_fire_outcome_compute.models.model_lab_fill_fire_outcome_policy import (
    ModelLabFillFireOutcomePolicy,
)


class ModelLabFillFireOutcomeRequest(BaseModel):
    """One fire as the tick observed it, with the policy to judge under.

    ``phase`` and ``exit`` are as the run left them, before any judgement.
    A selftest fire is not judged at all: its result is the input phase and
    exit unchanged. The two-pass caller delivers the first result's alert and
    judges again with ``alert_exit`` set to the delivery's exit code.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The overlay-loaded policy; the judge spells no setting of its own.
    policy: ModelLabFillFireOutcomePolicy
    #: The fire's id, as the launcher named it.
    fire_id: str
    #: The phase the run left the fire in.
    phase: str
    #: The exit code the run left the fire with.
    exit: int
    #: A selftest fire is returned unjudged.
    selftest: bool = False
    #: The fire had a workflow-result file named (the tick sets it for every
    #: non-selftest Workflow run).
    result_channel_enabled: bool
    #: The path named in the truncated notice ("" when none).
    workflow_result_file: str
    #: The run log carries the policy marker at the START of a line, followed
    #: by a space or end of line.
    truncated_marker_seen: bool
    #: Whether the fire's own written result file exists.
    file_state: Literal["absent", "present"]
    #: Its size is greater than zero.
    file_nonempty: bool
    #: Its content when present, else None.
    file_text: str | None
    #: The first run-log line matching ``policy.refused_pattern``, CR stripped
    #: and cut to ``policy.refused_line_max_chars`` ("" when none).
    run_log_refused_line: str
    #: Where the run log lives, named in the no-result alert.
    run_log_path: str
    #: Where the validated result is published.
    latest_path: str
    #: The Linear snapshot file of the day, and whether it exists.
    snapshot_name: str
    snapshot_present: bool
    #: None = the alert is not delivered yet; else the delivery's exit code.
    alert_exit: int | None = None


class ModelLabFillFireAlert(BaseModel):
    """The one alert the fire owes: its category and its rendered text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: EnumLabFillAlertCategory
    text: str


class ModelLabFillFireOutcomeResult(BaseModel):
    """The fire's receipt: what to record, what to send, what to publish."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The phase to record, after the judgement.
    phase: str
    #: The exit code to record, after the judgement.
    exit: int
    #: The receipt's degraded class, when the fire is degraded.
    degraded_class: str | None
    #: How the fire's own written result file read (None only for a selftest).
    result_channel: EnumLabFillResultChannel | None
    #: The completion notification was cut.
    result_truncated: bool
    #: A cut notification was overridden by the written result file.
    result_file_is_outcome: bool
    #: The notice line to log ("" when none).
    notice_line: str
    #: The alert to deliver (None when nothing is owed).
    alert: ModelLabFillFireAlert | None
    #: Echoes the request's ``alert_exit``.
    alert_exit: int | None
    #: The validated result to publish at ``latest_path``, else None.
    latest_value: dict[str, object] | None


__all__: list[str] = [
    "ModelLabFillFireAlert",
    "ModelLabFillFireOutcomeRequest",
    "ModelLabFillFireOutcomeResult",
]
