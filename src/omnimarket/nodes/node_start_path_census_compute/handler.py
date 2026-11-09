# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The start-path census: ``handle(ModelStartPathCensusRequest) -> ModelStartPathCensusResult`` (OMN-22940).

* Census: every start seen on a path the census does not list fails it, as does a start on a dead path.
* Drill: a planted bypassing start is refused only when its path is in ``enforce``. In ``observe`` it must be logged as
  a refusal and started; a path in ``enforce`` with no passing drill fails.
* A request tagged ``drill`` on the test instance is drill evidence and nothing else. It never clears, hides or
  downgrades an operational failure, and a drill tag off the test instance is itself an operational failure.
"""

from __future__ import annotations

from collections import defaultdict

from omnimarket.models.model_start_admission import (
    EnumAdmissionMode,
    EnumAdmissionOutcome,
)
from omnimarket.nodes.node_start_path_census_compute.models import (
    INSTANCE_TEST,
    TAG_DRILL,
    EnumDrillVerdict,
    EnumStartPathStatus,
    ModelDrillObservation,
    ModelStartPathCensusRequest,
    ModelStartPathCensusResult,
    ModelStartPathReading,
)


def _is_drill(tag: str, instance: str) -> bool:
    return tag == TAG_DRILL and instance == INSTANCE_TEST


def _drill_reasons(
    drill: ModelDrillObservation, enforcement: EnumAdmissionMode
) -> list[str]:
    reasons: list[str] = []
    if drill.mode is not enforcement:
        reasons.append(f"mode_mismatch:{drill.mode.value}!={enforcement.value}")
    if enforcement is EnumAdmissionMode.ENFORCE:
        if drill.outcome is not EnumAdmissionOutcome.REFUSED or drill.proceed:
            reasons.append(f"bypass_not_refused:{drill.outcome.value}")
    elif drill.outcome is EnumAdmissionOutcome.REFUSED:
        reasons.append("refused_in_observe")
    elif (
        drill.outcome is not EnumAdmissionOutcome.OBSERVED_REFUSAL or not drill.proceed
    ):
        reasons.append(f"bypass_not_detected:{drill.outcome.value}")
    return reasons


class HandlerStartPathCensus:
    """Read the census against the starts seen and the drills planted; touch nothing."""

    def handle(
        self, request: ModelStartPathCensusRequest
    ) -> ModelStartPathCensusResult:
        entries = {entry.path_id: entry for entry in request.entries}
        unlisted: set[str] = set()
        dead_started: set[str] = set()
        operational: list[str] = []
        drill_failures: list[str] = []
        drills_by_path: dict[str, list[ModelDrillObservation]] = defaultdict(list)

        def place(path_id: str) -> bool:
            """Record a start on a path; true when the path is listed and live."""
            entry = entries.get(path_id)
            if entry is None:
                unlisted.add(path_id)
                return False
            if entry.status is EnumStartPathStatus.DEAD:
                dead_started.add(path_id)
                return False
            return True

        for start in request.observed_starts:
            place(start.path_id)
            if start.tag == TAG_DRILL and start.instance != INSTANCE_TEST:
                operational.append(f"{start.path_id}:drill_tag_off_test_instance")
            if start.failure:
                if _is_drill(start.tag, start.instance):
                    drill_failures.append(
                        f"{start.path_id}:drill_start_failure:{start.failure}"
                    )
                else:
                    operational.append(f"{start.path_id}:{start.failure}")

        for drill in request.drills:
            listed_and_live = place(drill.path_id)
            if not _is_drill(drill.tag, drill.instance):
                operational.append(f"{drill.path_id}:drill_result_not_on_test_instance")
            elif listed_and_live:
                drills_by_path[drill.path_id].append(drill)

        readings: list[ModelStartPathReading] = []
        for entry in request.entries:
            enforcement = request.admission_policy.modes.get(
                entry.path_id, request.admission_policy.default_mode
            )
            if entry.status is EnumStartPathStatus.DEAD:
                readings.append(
                    ModelStartPathReading(
                        path_id=entry.path_id,
                        status=entry.status,
                        enforcement=enforcement,
                        drill=EnumDrillVerdict.NOT_APPLICABLE,
                    )
                )
                continue
            planted = drills_by_path.get(entry.path_id, [])
            reasons: list[str] = []
            for drill in planted:
                reasons.extend(_drill_reasons(drill, enforcement))
            if not planted and enforcement is EnumAdmissionMode.ENFORCE:
                reasons.append("enforce_without_drill")
            if reasons:
                verdict = EnumDrillVerdict.FAIL
            elif not planted:
                verdict = EnumDrillVerdict.NOT_RUN
            elif enforcement is EnumAdmissionMode.ENFORCE:
                verdict = EnumDrillVerdict.PASS
            else:
                verdict = EnumDrillVerdict.OBSERVING
            drill_failures.extend(f"{entry.path_id}:{reason}" for reason in reasons)
            readings.append(
                ModelStartPathReading(
                    path_id=entry.path_id,
                    status=entry.status,
                    binding=entry.binding,
                    enforcement=enforcement,
                    drill=verdict,
                    drill_reasons=tuple(reasons),
                )
            )

        return ModelStartPathCensusResult(
            correlation_id=request.correlation_id,
            passed=not (unlisted or dead_started or drill_failures or operational),
            paths=tuple(readings),
            unlisted_starts=tuple(sorted(unlisted)),
            dead_path_starts=tuple(sorted(dead_started)),
            drill_failures=tuple(drill_failures),
            operational_failures=tuple(operational),
        )
