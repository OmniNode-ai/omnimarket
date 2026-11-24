# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerBuildDelegationChecks: run the ten delegation checks once per dev build and name the merges of a new red (OMN-26621).

One rebuild-completed event of the dev lane is one build. The build id is the event's git sha: a second event for the same
sha runs nothing. Each check's probe is run once against the build and recorded as pass, fail or unavailable with the build id
and the merges the build added over the previous recorded build. A check whose last reading was a pass and that now fails is a
regression: one event in the shared event model, with the merges between the two builds as suspects, and one alarm through the
lab alarm delivery with the named owner. A check whose last reading was already a fail raises nothing; a check that could not
run reads unavailable and is never a regression. A missing owner refuses the alarm.

The suspect merges are first-parent commits read through ``ModelAttributionCandidate``, the shape the nightly test attribution
(node_test_selector_reading_compute) uses for the commits between two heads. They are candidates, never proof of cause.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.config.state_root import resolve_onex_state_root
from omnimarket.models.delegation_check_regression import (
    EnumChangeKind,
    EnumRegressionSource,
    ModelChange,
    ModelRegressionEvent,
    ModelRunRef,
    ModelSuspectMerge,
    regression_event_id,
)
from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryCondition,
    ModelLabAlarm,
    ModelLabAlarmDeliveryRequest,
)
from omnimarket.models.overlay_documents.model_build_delegation_checks_overlay import (
    ModelBuildDelegationChecksOverlay,
)
from omnimarket.models.test_selector_reading import ModelAttributionCandidate
from omnimarket.nodes.node_build_delegation_checks.adapters.bus_sink import (
    BusRegressionSink,
)
from omnimarket.nodes.node_build_delegation_checks.adapters.command_check_executor import (
    CommandCheckExecutor,
)
from omnimarket.nodes.node_build_delegation_checks.adapters.file_build_store import (
    FileBuildStore,
)
from omnimarket.nodes.node_build_delegation_checks.adapters.git_merge_source import (
    GitMergeSource,
)
from omnimarket.nodes.node_build_delegation_checks.adapters.inmemory_adapters import (
    RecordedResultsExecutor,
)
from omnimarket.nodes.node_build_delegation_checks.models.model_build_delegation_checks import (
    EnumBuildChecksStatus,
    EnumCheckReading,
    ModelBuildChecksResult,
    ModelBuildRecord,
    ModelCheckOutcome,
    ModelCheckRecord,
    ModelRebuildCompletedObserved,
    ModelRefusedAlarm,
)
from omnimarket.nodes.node_build_delegation_checks.overlay import load_overlay
from omnimarket.nodes.node_build_delegation_checks.protocols.protocol_build_checks import (
    ProtocolBuildStore,
    ProtocolCheckRunner,
    ProtocolMergeSource,
    ProtocolRegressionSink,
)

WORKFLOW = "build-delegation-checks"
CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
REPLAY_ENV = "ONEX_BUILD_DELEGATION_CHECKS_REPLAY"
OMNI_HOME_ENV = "OMNI_HOME"


def _suspect(candidate: ModelAttributionCandidate) -> ModelSuspectMerge:
    return ModelSuspectMerge(
        commit_sha=candidate.commit_sha,
        pr_number=candidate.pr_number,
        merged_at=candidate.merged_at,
    )


def _name(merge: ModelSuspectMerge) -> str:
    return f"#{merge.pr_number}" if merge.pr_number else merge.commit_sha[:12]


class _BuildChecksRun:
    """One build's checks over the five ports, which the handler passes in whole."""

    def __init__(
        self,
        *,
        settings: ModelBuildDelegationChecksOverlay,
        runner: ProtocolCheckRunner,
        store: ProtocolBuildStore,
        merges: ProtocolMergeSource,
        sink: ProtocolRegressionSink,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._runner = runner
        self._store = store
        self._merges = merges
        self._sink = sink
        self._clock = clock or (lambda: datetime.now(UTC))

    def handle(self, request: ModelRebuildCompletedObserved) -> ModelBuildChecksResult:
        settings = self._settings
        build_id = request.git_sha
        if request.runtime_lane != settings.dev_runtime_lane:
            return self._skipped(
                build_id, f"runtime lane {request.runtime_lane} is not the dev lane"
            )
        if request.status != "success":
            return self._skipped(build_id, "the rebuild did not succeed")

        now = self._clock()
        previous = self._store.previous_build(request.runtime_lane, build_id)
        previous_id = previous.build_id if previous else ""
        added = self._between(previous_id, build_id)
        begun = ModelBuildRecord(
            build_id=build_id,
            runtime_lane=request.runtime_lane,
            previous_build_id=previous_id,
            started_at=now,
            merges=added,
        )
        # The claim comes before any probe runs: a second event for this build, replayed or concurrent, finds it taken.
        if not self._store.begin(begun):
            return ModelBuildChecksResult(
                build_id=build_id,
                status=EnumBuildChecksStatus.ALREADY_RUN,
                reason="the build already has a record",
                previous_build_id=previous_id,
            )

        records: list[ModelCheckRecord] = []
        events: list[ModelRegressionEvent] = []
        alarms: list[ModelLabAlarmDeliveryRequest] = []
        refused: list[ModelRefusedAlarm] = []
        for check_id in settings.checks:
            outcome = self._run(check_id, build_id)
            last = self._store.last_reading(check_id, build_id)
            record = ModelCheckRecord(
                check_id=check_id,
                build_id=build_id,
                previous_build_id=previous_id,
                reading=outcome.reading,
                detail=outcome.detail,
                run_id=outcome.run_id,
                probe=outcome.probe,
                at=outcome.finished_at or now,
                merges=added,
            )
            records.append(record)
            if outcome.reading is not EnumCheckReading.FAIL:
                continue
            if last is None or last.reading is not EnumCheckReading.PASS:
                continue  # already red, or never read green: no new regression
            owner = (settings.owner or "").strip()
            if not owner:
                refused.append(
                    ModelRefusedAlarm(
                        check_id=check_id,
                        reason="owner-missing: the settings store names no owner, so the alarm is refused, not sent unowned",
                    )
                )
                continue
            suspects = (
                added
                if last.build_id == previous_id
                else self._between(last.build_id, build_id)
            )
            event = self._event(check_id, build_id, record, last, suspects, owner, now)
            events.append(event)
            alarms.append(self._alarm(event))

        result = ModelBuildChecksResult(
            build_id=build_id,
            status=EnumBuildChecksStatus.RAN,
            previous_build_id=previous_id,
            records=tuple(records),
            regressions=tuple(events),
            alarms=tuple(alarms),
            refused=tuple(refused),
        )
        self._store.finish(
            begun.model_copy(update={"checks": tuple(records), "complete": True})
        )
        failed: list[ModelRefusedAlarm] = []
        for event, alarm in zip(events, alarms, strict=True):
            # The alarm is attempted even when the event cannot be published: a refused topic must not silence the alarm.
            failed.extend(
                refusal
                for refusal in (
                    self._attempt(
                        event.check_id, "event", self._sink.publish_regression, event
                    ),
                    self._attempt(
                        event.check_id, "alarm", self._sink.request_alarm, alarm
                    ),
                )
                if refusal is not None
            )
        # One record per check on the per-check topic, so each of the ten checks has its own reading on the bus.
        failed.extend(
            refusal
            for refusal in (
                self._attempt(
                    record.check_id, "check", self._sink.publish_check_record, record
                )
                for record in records
            )
            if refusal is not None
        )
        try:
            self._sink.publish_result(result)
        except OSError as exc:
            failed.append(
                ModelRefusedAlarm(
                    check_id="*", reason=f"publish-failed: {type(exc).__name__}"
                )
            )
        if failed:
            result = result.model_copy(
                update={"refused": result.refused + tuple(failed)}
            )
        return result

    @staticmethod
    def _attempt[T](
        check_id: str, what: str, send: Callable[[T], None], payload: T
    ) -> ModelRefusedAlarm | None:
        try:
            send(payload)
        except OSError as exc:
            return ModelRefusedAlarm(
                check_id=check_id,
                reason=f"publish-failed ({what}): {type(exc).__name__}",
            )
        return None

    def _skipped(self, build_id: str, reason: str) -> ModelBuildChecksResult:
        return ModelBuildChecksResult(
            build_id=build_id, status=EnumBuildChecksStatus.SKIPPED, reason=reason
        )

    def _between(self, base_id: str, head_id: str) -> tuple[ModelSuspectMerge, ...]:
        if not base_id:
            return ()
        return tuple(_suspect(c) for c in self._merges.merges_between(base_id, head_id))

    def _run(self, check_id: str, build_id: str) -> ModelCheckOutcome:
        try:
            return self._runner.run(
                check_id, build_id, self._settings.check_timeout_seconds
            )
        except Exception as exc:
            return ModelCheckOutcome(
                reading=EnumCheckReading.UNAVAILABLE,
                detail=f"the probe could not run: {type(exc).__name__}",
            )

    def _event(
        self,
        check_id: str,
        build_id: str,
        red: ModelCheckRecord,
        green: ModelCheckRecord,
        suspects: tuple[ModelSuspectMerge, ...],
        owner: str,
        now: datetime,
    ) -> ModelRegressionEvent:
        latency = max(0.0, (now - red.at).total_seconds())
        return ModelRegressionEvent(
            event_id=regression_event_id(check_id, build_id),
            check_id=check_id,
            repository=self._settings.repository,
            workflow=WORKFLOW,
            last_green=ModelRunRef(
                run_id=green.run_id or green.build_id, at=green.at, ref=green.build_id
            ),
            first_red=ModelRunRef(
                run_id=red.run_id or build_id, at=red.at, ref=build_id
            ),
            changes=(
                ModelChange(
                    kind=EnumChangeKind.DEV_BUILD,
                    at=now,
                    ref=build_id,
                    summary=f"dev build {build_id[:12]} over {green.build_id[:12]}, {len(suspects)} merge(s) added",
                ),
            ),
            owner=owner,
            alarm_lane=self._settings.alarm_lane,
            detected_at=now,
            alarm_latency_seconds=latency,
            late=latency > self._settings.max_alarm_latency_seconds,
            source=EnumRegressionSource.DEV_BUILD,
            build_id=build_id,
            previous_build_id=green.build_id,
            suspects=suspects,
        )

    def _alarm(self, event: ModelRegressionEvent) -> ModelLabAlarmDeliveryRequest:
        names = (
            ", ".join(_name(m) for m in event.suspects)
            or "none (the build added no merge)"
        )
        detail = (
            f"{event.check_id} passed on build {event.previous_build_id[:12]} and fails on build {event.build_id[:12]}; "
            f"owner {event.owner}; suspect merges: {names}"
        )
        condition = self._settings.alarm_condition
        return ModelLabAlarmDeliveryRequest(
            correlation_id=event.event_id,
            condition=condition,
            outcome=EnumDeliveryCondition.ALARM,
            # One subject per regression: a check that recovers and goes red on a later build is a new episode.
            alarms=(
                ModelLabAlarm(
                    condition=condition, subject=event.event_id, detail=detail
                ),
            ),
            evidence=f"regression event {event.event_id}",
        )


def recorded_results(path: Path) -> RecordedResultsExecutor:
    """An executor over a recorded results file: ``{"builds": {build_id: {check_id: {reading, run_id, detail}}}}``."""
    raw = json.loads(path.read_text(encoding="utf-8"))["builds"]
    return RecordedResultsExecutor(
        {
            build: {
                check: ModelCheckOutcome.model_validate(item)
                for check, item in checks.items()
            }
            for build, checks in raw.items()
        }
    )


class HandlerBuildDelegationChecks:
    """EFFECT handler bound by the contract: the runtime builds it with no argument and its ports are made from the overlay on
    the first request. A caller that passes every port (the tests, a replay) gets exactly those ports; a partial set is refused."""

    def __init__(
        self,
        *,
        settings: ModelBuildDelegationChecksOverlay | None = None,
        runner: ProtocolCheckRunner | None = None,
        store: ProtocolBuildStore | None = None,
        merges: ProtocolMergeSource | None = None,
        sink: ProtocolRegressionSink | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        ports = (settings, runner, store, merges, sink)
        if any(p is not None for p in ports) and not all(p is not None for p in ports):
            raise ValueError(
                "pass every port (settings, runner, store, merges, sink) or none"
            )
        self._run: _BuildChecksRun | None = (
            _BuildChecksRun(
                settings=settings,
                runner=runner,
                store=store,
                merges=merges,
                sink=sink,
                clock=clock,
            )
            if settings is not None
            and runner is not None
            and store is not None
            and merges is not None
            and sink is not None
            else None
        )
        self._clock = clock

    def handle(self, request: ModelRebuildCompletedObserved) -> ModelBuildChecksResult:
        run = self._run if self._run is not None else self._from_overlay()
        return run.handle(request)

    def _from_overlay(self) -> _BuildChecksRun:
        topics: dict[str, str] = yaml.safe_load(
            CONTRACT_PATH.read_text(encoding="utf-8")
        )["topics"]
        settings = load_overlay()
        home = os.environ.get(OMNI_HOME_ENV, "").strip()
        if not home:
            raise OSError(f"{OMNI_HOME_ENV} names no registry root")
        root = Path(home)
        # The registry root is a read-only mount in the runtime; the build history belongs under the declared writable state root.
        state_root = resolve_onex_state_root(
            purpose="node_build_delegation_checks build history"
        )
        replay = os.environ.get(REPLAY_ENV, "").strip()
        runner: ProtocolCheckRunner = (
            recorded_results(Path(replay))
            if replay
            else CommandCheckExecutor(settings.check_routes, registry_root=root)
        )
        self._run = _BuildChecksRun(
            settings=settings,
            runner=runner,
            store=FileBuildStore(
                state_root / settings.history_relpath, settings.history_keep
            ),
            merges=GitMergeSource(root / settings.merge_clone_relpath),
            sink=BusRegressionSink(
                regression_topic=topics["regression_detected"],
                alarm_topic=topics["alarm_delivery_requested"],
                result_topic=topics["build_checks_recorded"],
                check_topic=topics["check_recorded"],
                bus="kafka",
                lane=settings.bus_lane,
                workspace=root,
            ),
            clock=self._clock,
        )
        return self._run


__all__ = [
    "OMNI_HOME_ENV",
    "REPLAY_ENV",
    "WORKFLOW",
    "HandlerBuildDelegationChecks",
    "recorded_results",
]
