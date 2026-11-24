# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A delegation check that goes green to red raises one regression event, one alarm and one fix lane.

One scan reads the finished runs of every watched check's workflow (the probes' own recorded results), finds each green
to red transition, and for each transition not yet raised:

* publishes one regression event naming the check, the last green run, the first red run and what changed between them
  (merges, dev builds, host or model setting changes);
* hands one alarm to the lab alarm delivery path, with the named owner and the orchestrator lane;
* queues one fix lane for the check unless a live CLAIM already holds it.

A flap (a red run that the next run turned green inside one interval) is counted from the same history; the second flap of
a check inside the flap window raises a flap alarm. Every id is derived from the check and the run, so a repeated scan,
a replay or a restarted node raises nothing twice.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import yaml
from pydantic import BaseModel

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryCondition,
    ModelLabAlarm,
    ModelLabAlarmDeliveryRequest,
)
from omnimarket.nodes.node_delegation_check_regression_effect.adapters.ports import (
    open_ports,
)
from omnimarket.nodes.node_delegation_check_regression_effect.models.model_regression import (
    EnumRunVerdict,
    ModelFixLaneRequest,
    ModelFlap,
    ModelFlapCount,
    ModelHeldFix,
    ModelProbeRun,
    ModelRegressionEvent,
    ModelRegressionScanRequest,
    ModelRegressionScanResult,
    ModelRegressionSettings,
    ModelRunRef,
    ModelUnreadCheck,
    ModelWatchedCheck,
)
from omnimarket.nodes.node_delegation_check_regression_effect.overlay import (
    load_settings,
)
from omnimarket.nodes.node_delegation_check_regression_effect.protocols import (
    ProbeRunsUnreadableError,
    ProtocolChangeSource,
    ProtocolLiveClaims,
    ProtocolProbeRunSource,
    ProtocolRegressionPublisher,
    ProtocolSeenStore,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract; handlers hold no topic string."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


def _ref(run: ModelProbeRun) -> ModelRunRef:
    return ModelRunRef(run_id=run.run_id, at=run.at, ref=run.ref)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%MZ")


def event_id_for(check_id: str, first_red_run_id: str) -> str:
    """One id per transition: the check and its first red run."""
    return str(
        uuid5(
            NAMESPACE_URL, f"delegation-check-regression:{check_id}:{first_red_run_id}"
        )
    )


def find_transitions(
    runs: list[ModelProbeRun],
) -> list[tuple[ModelProbeRun, ModelProbeRun, ModelProbeRun | None]]:
    """Each green to red transition of runs sorted oldest first: (last green, first red, the next green or None)."""
    found: list[tuple[ModelProbeRun, ModelProbeRun, ModelProbeRun | None]] = []
    for index in range(1, len(runs)):
        previous, current = runs[index - 1], runs[index]
        if (
            previous.verdict is EnumRunVerdict.PASS
            and current.verdict is EnumRunVerdict.FAIL
        ):
            recovered = next(
                (r for r in runs[index + 1 :] if r.verdict is EnumRunVerdict.PASS), None
            )
            found.append((previous, current, recovered))
    return found


def find_flaps(
    runs: list[ModelProbeRun], check: ModelWatchedCheck, grace_seconds: int
) -> list[ModelFlap]:
    """Each red episode that the next green run ended inside one interval (plus the grace) of its first red run."""
    bound = timedelta(seconds=check.interval_seconds + grace_seconds)
    return [
        ModelFlap(check_id=check.check_id, red=_ref(red), green=_ref(recovered))
        for _, red, recovered in find_transitions(runs)
        if recovered is not None and recovered.at - red.at <= bound
    ]


class RegressionScan:
    """One scan over given ports: read the probes' results, raise each new green to red transition and each second flap."""

    def __init__(
        self,
        *,
        settings: ModelRegressionSettings,
        runs: ProtocolProbeRunSource,
        changes: ProtocolChangeSource,
        claims: ProtocolLiveClaims,
        seen: ProtocolSeenStore,
        publisher: ProtocolRegressionPublisher,
        topics: dict[str, str] | None = None,
    ) -> None:
        self._settings = settings
        self._runs = runs
        self._changes = changes
        self._claims = claims
        self._seen = seen
        self._publisher = publisher
        self._topics = topics if topics is not None else load_topics()

    def handle(self, request: ModelRegressionScanRequest) -> ModelRegressionScanResult:
        settings = self._settings
        now = request.now
        selected = [
            c
            for c in settings.checks
            if not request.check_ids or c.check_id in request.check_ids
        ]
        events: list[ModelRegressionEvent] = []
        flap_counts: list[ModelFlapCount] = []
        fix_lanes: list[ModelFixLaneRequest] = []
        held: list[ModelHeldFix] = []
        unread: list[ModelUnreadCheck] = []
        skipped: list[str] = []
        failures: list[str] = []
        regression_alarms: list[ModelLabAlarm] = []
        fixed_checks: set[str] = set()
        window_start = now - timedelta(seconds=settings.flap_window_seconds)

        for check in selected:
            try:
                runs = sorted(
                    self._runs.runs(check, settings.runs_per_check), key=lambda r: r.at
                )
            except ProbeRunsUnreadableError as exc:
                unread.append(
                    ModelUnreadCheck(check_id=check.check_id, reason=str(exc)[:240])
                )
                continue
            flaps = [
                f
                for f in find_flaps(runs, check, settings.flap_grace_seconds)
                if window_start < f.green.at <= now
            ]
            flap_counts.append(
                ModelFlapCount(check_id=check.check_id, flaps=tuple(flaps))
            )
            owner = check.owner or settings.owner
            for green, red, recovered in find_transitions(runs):
                if now - red.at > timedelta(
                    seconds=settings.max_transition_age_seconds
                ):
                    continue
                event_id = event_id_for(check.check_id, red.run_id)
                if self._seen.seen(event_id):
                    skipped.append(event_id)
                    continue
                latency = (now - red.at).total_seconds()
                event = ModelRegressionEvent(
                    event_id=event_id,
                    check_id=check.check_id,
                    repository=check.repository,
                    workflow=check.workflow,
                    last_green=_ref(green),
                    first_red=_ref(red),
                    changes=self._changes.between(green.at, red.at),
                    owner=owner,
                    alarm_lane=settings.alarm_lane,
                    detected_at=now,
                    alarm_latency_seconds=latency,
                    late=latency > settings.max_alarm_latency_seconds,
                    recovered_at=recovered.at if recovered else None,
                )
                events.append(event)
                regression_alarms.append(
                    ModelLabAlarm(
                        condition=settings.regression_condition,
                        subject=f"{check.check_id}@{red.run_id}",
                        detail=self._regression_detail(event),
                    )
                )
                if check.check_id in fixed_checks:
                    continue
                fixed_checks.add(check.check_id)
                holder = self._claims.holder(check.check_id)
                if holder is not None:
                    held.append(
                        ModelHeldFix(
                            check_id=check.check_id, event_id=event_id, held_by=holder
                        )
                    )
                else:
                    fix_lanes.append(
                        ModelFixLaneRequest(
                            request_id=str(
                                uuid5(
                                    NAMESPACE_URL,
                                    f"delegation-check-fix-lane:{check.check_id}:{red.run_id}",
                                )
                            ),
                            check_id=check.check_id,
                            event_id=event_id,
                            owner=owner,
                            alarm_lane=settings.alarm_lane,
                            first_red=_ref(red),
                        )
                    )

        alarms = self._alarm_requests(now, regression_alarms, flap_counts)
        if request.write:
            failures = self._write(events, alarms, fix_lanes)
            if not failures:
                for event in events:
                    self._seen.record(event.event_id)
        result = ModelRegressionScanResult(
            now=now,
            events=tuple(events),
            flap_counts=tuple(flap_counts),
            alarms=tuple(alarms),
            fix_lanes=tuple(fix_lanes),
            held_fixes=tuple(held),
            unread=tuple(unread),
            skipped_seen=tuple(skipped),
            publish_failures=tuple(failures),
        )
        if request.write:
            key = str(
                uuid5(
                    NAMESPACE_URL, f"delegation-check-regression-scan:{now.isoformat()}"
                )
            )
            try:
                self._publisher.publish(self._topics["scan_completed"], key, result)
            except OSError as exc:
                return result.model_copy(
                    update={
                        "publish_failures": (
                            *result.publish_failures,
                            f"scan_completed {key}: {type(exc).__name__}",
                        )
                    }
                )
        return result

    def _regression_detail(self, event: ModelRegressionEvent) -> str:
        changed = (
            "; ".join(
                f"{c.kind.value} {_stamp(c.at)} {c.summary}" for c in event.changes
            )
            or "no change read between the two runs"
        )
        return (
            f"{event.check_id} went green to red: last green {event.last_green.run_id} {_stamp(event.last_green.at)}, "
            f"first red {event.first_red.run_id} {_stamp(event.first_red.at)}; changed between: {changed}; "
            f"owner {event.owner}, to {event.alarm_lane}"
        )

    def _alarm_requests(
        self,
        now: datetime,
        regression_alarms: list[ModelLabAlarm],
        flap_counts: list[ModelFlapCount],
    ) -> list[ModelLabAlarmDeliveryRequest]:
        settings = self._settings
        requests: list[ModelLabAlarmDeliveryRequest] = []
        if regression_alarms:
            ids = sorted(a.subject for a in regression_alarms)
            requests.append(
                ModelLabAlarmDeliveryRequest(
                    correlation_id=str(
                        uuid5(NAMESPACE_URL, "regression-alarm:" + "|".join(ids))
                    ),
                    condition=settings.regression_condition,
                    outcome=EnumDeliveryCondition.ALARM,
                    alarms=tuple(regression_alarms),
                    evidence=f"raised to {settings.alarm_lane}, owner {settings.owner}",
                )
            )
        flapping = [f for f in flap_counts if f.count >= settings.flap_alarm_count]
        if flapping:
            alarms = tuple(
                ModelLabAlarm(
                    condition=settings.flap_condition,
                    subject=f.check_id,
                    detail=(
                        f"{f.check_id} flapped {f.count} times in {settings.flap_window_seconds // 3600}h "
                        f"(red then green inside one interval), latest red {f.flaps[-1].red.run_id} "
                        f"{_stamp(f.flaps[-1].red.at)}; owner {settings.owner}, to {settings.alarm_lane}"
                    ),
                )
                for f in flapping
            )
            requests.append(
                ModelLabAlarmDeliveryRequest(
                    correlation_id=str(
                        uuid5(
                            NAMESPACE_URL,
                            "flap-alarm:"
                            + "|".join(
                                f"{f.check_id}:{f.flaps[-1].red.run_id}"
                                for f in flapping
                            ),
                        )
                    ),
                    condition=settings.flap_condition,
                    outcome=EnumDeliveryCondition.ALARM,
                    alarms=alarms,
                    evidence=f"raised to {settings.alarm_lane}, owner {settings.owner}",
                )
            )
        else:
            requests.append(
                ModelLabAlarmDeliveryRequest(
                    correlation_id=str(
                        uuid5(NAMESPACE_URL, f"flap-clear:{_stamp(now)}")
                    ),
                    condition=settings.flap_condition,
                    outcome=EnumDeliveryCondition.OK,
                )
            )
        return requests

    def _write(
        self,
        events: list[ModelRegressionEvent],
        alarms: list[ModelLabAlarmDeliveryRequest],
        fix_lanes: list[ModelFixLaneRequest],
    ) -> list[str]:
        failures: list[str] = []
        steps: list[tuple[str, str, str, BaseModel]] = []
        for event in events:
            steps.append(
                (
                    "regression_detected",
                    self._topics["regression_detected"],
                    event.event_id,
                    event,
                )
            )
        for alarm in alarms:
            steps.append(
                (
                    "alarm",
                    self._topics["alarm_delivery_requested"],
                    alarm.correlation_id,
                    alarm,
                )
            )
        for fix in fix_lanes:
            steps.append(
                ("fix_lane", self._topics["fix_lane_queued"], fix.request_id, fix)
            )
        for label, topic, key, payload in steps:
            try:
                self._publisher.publish(topic, key, payload)
            except OSError as exc:
                failures.append(f"{label} {key}: {type(exc).__name__}")
        return failures


class HandlerDelegationCheckRegression:
    """EFFECT handler bound by the contract: the runtime builds it with no argument and its ports are made from the node's
    overlay on each request. A caller that passes every port (the tests, a replay) gets exactly those ports; a partial set is
    refused."""

    def __init__(
        self,
        *,
        settings: ModelRegressionSettings | None = None,
        runs: ProtocolProbeRunSource | None = None,
        changes: ProtocolChangeSource | None = None,
        claims: ProtocolLiveClaims | None = None,
        seen: ProtocolSeenStore | None = None,
        publisher: ProtocolRegressionPublisher | None = None,
        topics: dict[str, str] | None = None,
    ) -> None:
        self._scan: RegressionScan | None = None
        self._topics = topics
        given = (settings, runs, changes, claims, seen, publisher)
        if any(p is not None for p in given):
            if (
                settings is None
                or runs is None
                or changes is None
                or claims is None
                or seen is None
                or publisher is None
            ):
                raise ValueError(
                    "pass every port (settings, runs, changes, claims, seen, publisher) or none"
                )
            self._scan = RegressionScan(
                settings=settings,
                runs=runs,
                changes=changes,
                claims=claims,
                seen=seen,
                publisher=publisher,
                topics=topics,
            )

    def handle(self, request: ModelRegressionScanRequest) -> ModelRegressionScanResult:
        if self._scan is not None:
            return self._scan.handle(request)
        settings = load_settings()
        if settings.registry_root is None:
            raise NodeOverlayError(
                "the overlay names no registry_root, so the scan has nowhere to read the clones, the ledger and the state"
            )
        with open_ports(
            settings,
            now=request.now,
            registry_root=settings.registry_root,
            bus="kafka",
            replay=False,
        ) as ports:
            return RegressionScan(
                settings=settings,
                runs=ports.runs,
                changes=ports.changes,
                claims=ports.claims,
                seen=ports.seen,
                publisher=ports.publisher,
                topics=self._topics,
            ).handle(request)
