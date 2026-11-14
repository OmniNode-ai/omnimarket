# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev-tag-heartbeat condition: the deploy agent's heartbeat is fresh and unalarmed for every repository.

The old lab alarm's dev-tag heartbeat, ported with its rule unchanged: a beat that is missing, stale or
alarmed is a breach. A repository alarms only after its streak of consecutive breaching runs reaches the
overlay's persistence bound; before that the run is OK with a waiting reason. An unreadable source, a
document with no heartbeat object, or a malformed beat is INDETERMINATE, never ALARM and never OK.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryCondition,
    EnumDeliveryOutcome,
    ModelLabAlarm,
    ModelLabAlarmDeliveryRequest,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_dev_tag_heartbeat import (
    EnumDevTagHeartbeatFault,
    EnumDevTagHeartbeatOutcome,
    ModelDevTagHeartbeatCommand,
    ModelDevTagHeartbeatResult,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelDevTagHeartbeatSettings
from omnimarket.nodes.node_lab_alarm.protocols import (
    HeartbeatUnreadableError,
    ProtocolAlarmDelivery,
    ProtocolGrowthStreakStore,
    ProtocolHeartbeatSource,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"

CONDITION = "dev_tag_heartbeat"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


class HandlerDevTagHeartbeat:
    """Read the deploy agent's heartbeat; a repository alarms when its streak reaches the bound."""

    def __init__(
        self,
        *,
        settings: ModelDevTagHeartbeatSettings,
        source: ProtocolHeartbeatSource,
        streaks: ProtocolGrowthStreakStore,
        delivery: ProtocolAlarmDelivery | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._source = source
        self._streaks = streaks
        self._delivery = delivery
        self._clock = clock

    def handle(
        self, request: ModelDevTagHeartbeatCommand
    ) -> ModelDevTagHeartbeatResult:
        repositories = self._settings.repositories
        if request.fault is EnumDevTagHeartbeatFault.STALE_BEAT:
            first = repositories[0]
            detail = (
                f"the lane is behind {first}'s newest dev tag (injected: stale-beat): "
                f"running (injected), behind for (injected)s, past the agent's (injected)s bound"
            )
            return self._finish(
                request,
                EnumDevTagHeartbeatOutcome.ALARM,
                repositories,
                (),
                (f"dev-tag-heartbeat:{first}: {detail}",),
                ("injected: stale-beat",),
                detail,
            )
        try:
            document = self._source.read()
        except HeartbeatUnreadableError as exc:
            return self._finish(
                request,
                EnumDevTagHeartbeatOutcome.INDETERMINATE,
                repositories,
                (),
                (),
                ("heartbeat-unreadable",),
                f"the deploy agent's heartbeat could not be read: {exc}",
            )
        heartbeat = document.get("heartbeat")
        if not isinstance(heartbeat, Mapping):
            return self._finish(
                request,
                EnumDevTagHeartbeatOutcome.INDETERMINATE,
                repositories,
                (),
                (),
                ("no-heartbeat-object",),
                "the deploy agent's document has no heartbeat mapping",
            )
        now = self._clock()
        stored = self._streaks.load()
        alarms: list[str] = []
        reasons: list[str] = []
        readings: list[str] = []
        breaching: dict[str, int] = {}
        for repo in repositories:
            beat = heartbeat.get(repo)
            if not isinstance(beat, Mapping):
                detail = (
                    f"the deploy agent has no beat for {repo}, so nothing is checking "
                    f"the lane against its newest dev tag"
                )
                readings.append(f"{repo}: no beat")
                streak = stored.get(repo, 0) + 1
                breaching[repo] = streak
                self._classify(repo, streak, detail, alarms, reasons)
                continue
            observed_at = beat.get("observed_at")
            if not isinstance(observed_at, str):
                readings.append(f"{repo}: malformed observed_at")
                return self._finish(
                    request,
                    EnumDevTagHeartbeatOutcome.INDETERMINATE,
                    repositories,
                    tuple(readings),
                    (),
                    ("beat-malformed",),
                    f"the beat for {repo} has no parseable observed_at",
                )
            try:
                observed = datetime.fromisoformat(observed_at)
            except ValueError:
                readings.append(f"{repo}: malformed observed_at")
                return self._finish(
                    request,
                    EnumDevTagHeartbeatOutcome.INDETERMINATE,
                    repositories,
                    tuple(readings),
                    (),
                    ("beat-malformed",),
                    f"the beat for {repo} has no parseable observed_at",
                )
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=UTC)
            age = (now - observed).total_seconds()
            if age > self._settings.max_age_seconds:
                detail = (
                    f"the dev-tag heartbeat for {repo} is stale: last observed {observed_at}, "
                    f"{age:.0f}s ago, past the {self._settings.max_age_seconds:g}s bound"
                )
                readings.append(f"{repo}: stale ({age:.0f}s)")
                streak = stored.get(repo, 0) + 1
                breaching[repo] = streak
                self._classify(repo, streak, detail, alarms, reasons)
                continue
            if beat.get("alarm") is True:
                newest_tag = beat.get("newest_tag", "")
                running_ref = beat.get("running_ref", "")
                lag_seconds = beat.get("lag_seconds", 0)
                alarm_after_seconds = beat.get("alarm_after_seconds", 0)
                detail = (
                    f"the lane is behind {repo}'s newest dev tag {newest_tag}: "
                    f"running {running_ref}, behind for {lag_seconds}s, "
                    f"past the agent's {alarm_after_seconds}s bound"
                )
                readings.append(f"{repo}: alarmed")
                streak = stored.get(repo, 0) + 1
                breaching[repo] = streak
                self._classify(repo, streak, detail, alarms, reasons)
                continue
            readings.append(f"{repo}: fresh")
        if request.write:
            to_save = dict(stored)
            for repo in repositories:
                if repo in breaching:
                    to_save[repo] = breaching[repo]
                else:
                    to_save.pop(repo, None)
            self._streaks.save(to_save)
        outcome = (
            EnumDevTagHeartbeatOutcome.ALARM
            if alarms
            else EnumDevTagHeartbeatOutcome.OK
        )
        evidence = "; ".join(readings) if readings else "no repositories declared"
        return self._finish(
            request,
            outcome,
            repositories,
            tuple(readings),
            tuple(alarms),
            tuple(reasons),
            evidence,
        )

    def _classify(
        self,
        repo: str,
        streak: int,
        detail: str,
        alarms: list[str],
        reasons: list[str],
    ) -> None:
        bound = self._settings.persistence.persist_runs
        if streak >= bound:
            alarms.append(f"dev-tag-heartbeat:{repo}: {detail}")
        else:
            reasons.append(f"waiting:{repo}: run {streak} of {bound}")

    def _finish(
        self,
        request: ModelDevTagHeartbeatCommand,
        outcome: EnumDevTagHeartbeatOutcome,
        repositories: tuple[str, ...],
        readings: tuple[str, ...],
        alarms: tuple[str, ...],
        reasons: tuple[str, ...],
        evidence: str,
    ) -> ModelDevTagHeartbeatResult:
        failure = ""
        if outcome is EnumDevTagHeartbeatOutcome.ALARM:
            failure = f"alarm:{CONDITION}" + "".join(f";{r}" for r in reasons)
        elif outcome is EnumDevTagHeartbeatOutcome.INDETERMINATE:
            failure = "indeterminate:" + ";".join(reasons)
        result = ModelDevTagHeartbeatResult(
            correlation_id=request.correlation_id,
            outcome=outcome,
            fault=request.fault,
            repositories=repositories,
            readings=readings,
            alarms=alarms,
            reasons=reasons,
            evidence=evidence,
            failure=failure,
        )
        return self._deliver(request, result)

    def _deliver(
        self, request: ModelDevTagHeartbeatCommand, result: ModelDevTagHeartbeatResult
    ) -> ModelDevTagHeartbeatResult:
        """Hand the verdict to the alarm delivery; a probe and a drill never deliver.

        An INDETERMINATE or OK verdict is handed over too, as HandlerBrokerLag does: an OK is what
        clears an episode that an alarm opened. A delivery that fails or cannot run is named in the
        result's failure, so the run ends as an alarm meanwhile.
        """
        if (
            self._delivery is None
            or not request.write
            or request.fault is not EnumDevTagHeartbeatFault.NONE
        ):
            return result
        try:
            typed = []
            for line in result.alarms:
                subject, separator, detail = line.partition(": ")
                if not separator:
                    raise ValueError(
                        f"alarm line {line!r} names no subject before ': '"
                    )
                typed.append(
                    ModelLabAlarm(condition=CONDITION, subject=subject, detail=detail)
                )
            reply = self._delivery.handle(
                ModelLabAlarmDeliveryRequest(
                    correlation_id=request.correlation_id,
                    condition=CONDITION,
                    outcome=EnumDeliveryCondition(result.outcome.value),
                    alarms=tuple(typed),
                    evidence=result.evidence,
                )
            )
        except (ValueError, OSError) as exc:
            return self._with_delivery_failure(
                result, (), f"delivery-unavailable:{type(exc).__name__}"
            )
        entries = tuple(
            f"{d.key} channel={d.channel.value} ledger={d.ledger.value}"
            for d in reply.deliveries
        )
        if reply.outcome is EnumDeliveryOutcome.FAILED:
            return self._with_delivery_failure(
                result, entries, f"delivery-failed:{reply.failure}"
            )
        return result.model_copy(update={"delivery": entries})

    @staticmethod
    def _with_delivery_failure(
        result: ModelDevTagHeartbeatResult, entries: tuple[str, ...], failure: str
    ) -> ModelDevTagHeartbeatResult:
        return result.model_copy(
            update={
                "delivery": entries,
                "failure": f"{result.failure}; {failure}"
                if result.failure
                else failure,
            }
        )


def events_for(
    result: ModelDevTagHeartbeatResult, topics: Mapping[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The chain of one run: started, then the completed or the failed terminal."""
    cid = result.correlation_id
    chain: list[tuple[str, dict[str, object]]] = [
        (topics["dev_tag_heartbeat_run_started"], {"correlation_id": cid})
    ]
    final = topics[
        "dev_tag_heartbeat_run_completed"
        if result.outcome is EnumDevTagHeartbeatOutcome.OK
        else "dev_tag_heartbeat_run_failed"
    ]
    chain.append(
        (
            final,
            {
                "correlation_id": cid,
                "outcome": result.outcome.value,
                "fault": result.fault.value,
                "repositories": list(result.repositories),
                "reasons": list(result.reasons),
                "failure": result.failure,
            },
        )
    )
    return chain


__all__ = [
    "CONDITION",
    "HandlerDevTagHeartbeat",
    "events_for",
    "load_topics",
]
