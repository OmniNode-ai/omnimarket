# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The policy of the lab-fill fire outcome judge (OMN-24109).

Every field is required and carries no default: the values come from the
launcher's overlay, which the effect node ``node_lab_fill_launcher_effect``
loads, and none may be spelled in code. The judge reads the policy only off
the request.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelLabFillFireOutcomePolicy(BaseModel):
    """Every setting the fire outcome judgement applies, ride-on-request.

    The templates are ``str.format`` templates; the placeholder each one uses
    is named in its comment. ``no_result_phases`` lists the phases that earn an
    alert when the fire's result file does not exist.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The workflow whose fires this policy judges ("lab-fill").
    workflow: str = Field(min_length=1)
    #: The result-file schema version a written result must carry.
    results_version: int
    #: The first word of the line the launcher agent prints when the
    #: completion notification was cut.
    truncated_marker: str
    #: An extended regex, matched case-insensitively against run-log lines.
    refused_pattern: str
    #: The longest a refused line may get before it is judged.
    refused_line_max_chars: int = Field(ge=1)
    #: The longest the ``{alert_text}`` an idle alert embeds may be.
    alert_text_max_chars: int = Field(ge=1)
    #: Phases that get an alert when no result file exists.
    no_result_phases: tuple[str, ...]
    #: The exit code of a degraded fire.
    degraded_exit: int
    #: The exit code of a fire whose result is missing or invalid.
    invalid_exit: int
    #: The degraded class of a fire whose completion notification was cut.
    class_truncated: str
    #: The degraded class of a fire whose run log shows a refusal.
    class_refused: str
    #: The degraded class of a fire that wrote no result file.
    class_no_result: str
    #: The degraded class of a fire that filled nothing (blind fill).
    class_blind: str
    #: The degraded class of a fire whose alert was never delivered.
    class_undelivered: str
    #: The degraded class of a valid result with no friction record owed.
    class_friction_missing: str
    #: The cut-notification notice; uses ``{workflow}`` ``{degraded_class}``
    #: ``{result_file}``.
    truncated_notice_template: str
    #: The refusal alert; uses ``{fire_id}`` ``{refused_line}``.
    refused_alert_template: str
    #: The no-result alert; uses ``{fire_id}`` ``{phase}`` ``{exit}``
    #: ``{run_log}``.
    no_result_alert_template: str
    #: The idle alert; uses ``{cause}`` ``{free_slots}`` ``{eligible}``
    #: ``{dispatched}`` ``{latest}`` ``{alert_text}``.
    idle_alert_template: str
    #: The idle cause when the day's snapshot is missing; uses
    #: ``{snapshot_name}``.
    snapshot_cause_template: str
    #: What ``{eligible}`` reads when the result carries no ``eligible`` key.
    eligible_unread: str


__all__: list[str] = [
    "ModelLabFillFireOutcomePolicy",
]
