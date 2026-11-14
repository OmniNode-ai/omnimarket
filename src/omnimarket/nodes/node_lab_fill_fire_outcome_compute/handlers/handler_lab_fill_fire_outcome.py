# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLabFillFireOutcome: the pure judge of one lab-fill fire.

``handle(request: ModelLabFillFireOutcomeRequest) -> ModelLabFillFireOutcomeResult``.
No clock, no I/O, no model, no envelope: the tick has already read the run
log, the fire's own written result file and the delivery record, and hands
them over as facts. The same facts give the same receipt every time.

The judgement is the lab-fill outcome logic of ``morning_workflows_tick.sh``,
in the order that tick applies it:

1. A selftest fire is returned unjudged: the input phase and exit, no
   channel, no alert.
2. The cut notification (OMN-22431): a fire the result channel says
   completed, whose run log carries the policy's truncated marker, is
   degraded -- UNLESS the fire's own written result file exists and is
   non-empty, in which case the written file is the outcome and the fire is
   NOT degraded here.
3. The fire's own written result file: absent, it turns a completed fire
   result-invalid and earns an alert only for a phase the policy lists; a
   present one must parse as a JSON object and pass every schema rule, else
   the fire is result-invalid. A valid result is read into ``latest_value``,
   an idle or a blind verdict earns an alert, and a blind verdict, an
   undelivered alert or a missing friction record then degrades or
   invalidates the receipt, each assignment overriding the previous.

The node carries no setting: every marker, pattern, class name, exit code
and template rides on the request's policy, which the effect node
``node_lab_fill_launcher_effect`` loads from its overlay. The caller uses it
in two passes -- the first with ``alert_exit=None`` learns the alert, the
second, judged with the delivery's exit code, is final. Both are pure.
"""

from __future__ import annotations

import json
import re

from omnimarket.models.lab_fill.enum_lab_fill_fire_outcome import (
    EnumLabFillAlertCategory,
    EnumLabFillResultChannel,
)
from omnimarket.models.lab_fill.model_lab_fill_fire_outcome import (
    ModelLabFillFireAlert,
    ModelLabFillFireOutcomeRequest,
    ModelLabFillFireOutcomeResult,
)
from omnimarket.models.lab_fill.model_lab_fill_fire_outcome_policy import (
    ModelLabFillFireOutcomePolicy,
)

__all__: list[str] = [
    "HandlerLabFillFireOutcome",
    "first_refused_line",
    "first_word_marker_seen",
]


def first_refused_line(log_text: str, policy: ModelLabFillFireOutcomePolicy) -> str:
    """The first run-log line the policy's refused pattern matches.

    Matched case-insensitively, one line at a time; the line comes back CR
    stripped and cut to ``policy.refused_line_max_chars``. "" when none.
    """
    for line in log_text.splitlines():
        if re.search(policy.refused_pattern, line, re.IGNORECASE):
            return line.rstrip("\r")[: policy.refused_line_max_chars]
    return ""


def first_word_marker_seen(log_text: str, marker: str) -> bool:
    """Whether the run log carries the marker as a line's first word.

    True when any line equals the marker, or starts with the marker followed
    by a single space; a marker buried later in a line does not count.
    """
    return any(
        line == marker or line.startswith(f"{marker} ")
        for line in log_text.splitlines()
    )


def _parse_result(file_text: str | None) -> dict[str, object] | None:
    """The result file's content as a JSON object, else None."""
    if file_text is None:
        return None
    try:
        parsed: object = json.loads(file_text)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _validate_result(
    value: dict[str, object],
    fire_id: str,
    policy: ModelLabFillFireOutcomePolicy,
) -> bool:
    """Every schema rule a written lab-fill result must pass.

    A missing key, a wrongly typed value or a broken invariant fails the
    whole result (a bool is not an int, so ``free_slots: true`` is invalid).
    """
    if value.get("version") != policy.results_version:
        return False
    if value.get("workflow") != policy.workflow:
        return False
    if value.get("fire_id") != fire_id:
        return False
    free_slots = value.get("free_slots")
    dispatched = value.get("dispatched")
    if type(free_slots) is not int or free_slots < 0:
        return False
    if type(dispatched) is not int or dispatched < 0:
        return False
    idle_alarm = value.get("idle_alarm")
    friction_recorded = value.get("friction_recorded")
    if type(idle_alarm) is not bool or type(friction_recorded) is not bool:
        return False
    if idle_alarm != (free_slots > 0 and dispatched == 0):
        return False
    for key in ("observed_at", "session_line", "alert_text"):
        text = value.get(key)
        if not isinstance(text, str) or "\n" in text or "\r" in text:
            return False
    if idle_alarm and not value["alert_text"]:
        return False
    enumeration_failed = value.get("enumeration_failed", False)
    if type(enumeration_failed) is not bool:
        return False
    return not enumeration_failed or idle_alarm


class HandlerLabFillFireOutcome:
    """The pure judge of one lab-fill fire (no clock, no I/O)."""

    def handle(
        self, request: ModelLabFillFireOutcomeRequest
    ) -> ModelLabFillFireOutcomeResult:
        """Judge one fire and return its receipt."""
        policy = request.policy
        if request.selftest:
            return ModelLabFillFireOutcomeResult(
                phase=request.phase,
                exit=request.exit,
                degraded_class=None,
                result_channel=None,
                result_truncated=False,
                result_file_is_outcome=False,
                notice_line="",
                alert=None,
                alert_exit=request.alert_exit,
                latest_value=None,
            )

        phase = request.phase
        exit_code = request.exit
        degraded_class: str | None = None
        notice = ""
        truncated = False
        file_is_outcome = False

        # Stage 1: the cut completion notification. A written, non-empty
        # result file is the outcome and overrides it (OMN-22431).
        if (
            request.result_channel_enabled
            and phase == "completed"
            and request.truncated_marker_seen
        ):
            truncated = True
            if request.file_nonempty:
                file_is_outcome = True
            else:
                phase = "degraded"
                exit_code = policy.degraded_exit
                degraded_class = policy.class_truncated
                notice = policy.truncated_notice_template.format(
                    workflow=policy.workflow,
                    degraded_class=degraded_class,
                    result_file=request.workflow_result_file,
                )

        alert: ModelLabFillFireAlert | None = None
        latest_value: dict[str, object] | None = None

        if request.file_state == "absent":
            # Stage 2A: no written result file. A phase the policy does not
            # list (including one just set to result_invalid or degraded)
            # earns no alert.
            channel = EnumLabFillResultChannel.ABSENT
            if phase == "completed":
                phase = "result_invalid"
                exit_code = policy.invalid_exit
            if phase in policy.no_result_phases:
                if request.run_log_refused_line:
                    alert = ModelLabFillFireAlert(
                        category=EnumLabFillAlertCategory.REFUSED,
                        text=policy.refused_alert_template.format(
                            fire_id=request.fire_id,
                            refused_line=request.run_log_refused_line,
                        ),
                    )
                    degraded_class = policy.class_refused
                else:
                    alert = ModelLabFillFireAlert(
                        category=EnumLabFillAlertCategory.NO_RESULT,
                        text=policy.no_result_alert_template.format(
                            fire_id=request.fire_id,
                            phase=phase,
                            exit=exit_code,
                            run_log=request.run_log_path,
                        ),
                    )
                    degraded_class = policy.class_no_result
                notice = alert.text
        else:
            # Stage 2B: the fire's own written result file.
            value = _parse_result(request.file_text)
            if value is not None and _validate_result(value, request.fire_id, policy):
                channel = EnumLabFillResultChannel.READ
                latest_value = value
                blind = bool(value.get("enumeration_failed", False))
                idle_alarm = bool(value["idle_alarm"])
                if blind:
                    alert = ModelLabFillFireAlert(
                        category=EnumLabFillAlertCategory.BLIND,
                        text=str(value["alert_text"]),
                    )
                elif idle_alarm:
                    cause = (
                        ""
                        if request.snapshot_present
                        else policy.snapshot_cause_template.format(
                            snapshot_name=request.snapshot_name
                        )
                    )
                    alert = ModelLabFillFireAlert(
                        category=EnumLabFillAlertCategory.IDLE,
                        text=policy.idle_alert_template.format(
                            cause=cause,
                            free_slots=value["free_slots"],
                            eligible=value.get("eligible", policy.eligible_unread),
                            dispatched=value["dispatched"],
                            latest=request.latest_path,
                            alert_text=str(value["alert_text"])[
                                : policy.alert_text_max_chars
                            ],
                        ),
                    )
                # Each assignment overrides the previous.
                friction_ok = bool(value["friction_recorded"]) or not idle_alarm
                if blind:
                    phase = "degraded"
                    exit_code = policy.degraded_exit
                    degraded_class = policy.class_blind
                if (
                    alert is not None
                    and request.alert_exit is not None
                    and request.alert_exit != 0
                ):
                    phase = "degraded"
                    exit_code = policy.degraded_exit
                    degraded_class = policy.class_undelivered
                if not friction_ok:
                    phase = "result_invalid"
                    exit_code = policy.invalid_exit
                    degraded_class = policy.class_friction_missing
            else:
                channel = EnumLabFillResultChannel.INVALID
                phase = "result_invalid"
                exit_code = policy.invalid_exit

        return ModelLabFillFireOutcomeResult(
            phase=phase,
            exit=exit_code,
            degraded_class=degraded_class,
            result_channel=channel,
            result_truncated=truncated,
            result_file_is_outcome=file_is_outcome,
            notice_line=notice,
            alert=alert,
            alert_exit=request.alert_exit,
            latest_value=latest_value,
        )
