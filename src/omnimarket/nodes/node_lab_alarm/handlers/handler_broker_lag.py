# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The broker lag condition: derive the live consumer groups from the runtime's contracts, apply the growth rule.

The runtime mints one consumer group per subscribed topic from each node's own contract identity,
``{env}.{package}.{node}.consume.{contract_version}.__i.{instance}.__t.{topic}``. A literal list of those
names goes stale when a contract version bumps or a writer is folded into the runtime, and the alarm then
reads groups nobody consumes (OMN-23024: the delegation writer's standalone group had no members for hours
while the runtime's groups carried the lag). So the watched set is the running image's own answer: the
overlay names the nodes, the manifest the image serves names the groups. A declared node the manifest does
not carry, or a manifest that cannot be read, is INDETERMINATE with the reason, never a smaller list: an
alarm that cannot name its subjects has not read zero lag.

The growth rule is the alarm script's, unchanged: a group that grew between two consecutive samples alarms,
a high flat lag does not. A group that held a committed offset and now holds none has vanished, which alarms.
A never-committed group is informational and does not block OK. An unreadable group is INDETERMINATE.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from omnimarket.events.lab_alarm_delivery import (
    EnumDeliveryCondition,
    EnumDeliveryOutcome,
    ModelLabAlarm,
    ModelLabAlarmDeliveryRequest,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm import (
    EnumBrokerLagOutcome,
    EnumGroupState,
    EnumLabAlarmFault,
    ModelBrokerLagCommand,
    ModelBrokerLagResult,
    ModelGroupReading,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelLabAlarmOverlay
from omnimarket.nodes.node_lab_alarm.protocols import (
    GroupNeverCommittedError,
    GroupUnreadableError,
    ManifestUnreachableError,
    ManifestUnreadableError,
    ProtocolAlarmDelivery,
    ProtocolGroupLagReader,
    ProtocolGrowthStreakStore,
    ProtocolManifestCache,
    ProtocolManifestSource,
    ProtocolRunRecordStore,
    ProtocolSampleStore,
    RunRecordUnwritableError,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"

_GROUP_MAX = 255
_INVALID = re.compile(r"[^a-z0-9._-]")
_SEPARATORS = re.compile(r"[._-]{2,}")
_EDGES = re.compile(r"^[._-]+|[._-]+$")
_INSTANCE_INFIX = ".__i."
_TOPIC_INFIX = ".__t."
_PURPOSE = "consume"
_PLANTED_GROWTH = 1000
_UNKNOWN_NODE = ("planted-package", "planted_node_the_image_does_not_carry")


class ConsumerGroupDerivationError(Exception):
    """The watched groups could not be derived from the running image's manifest."""


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


def normalize_kafka_identifier(value: str) -> str:
    """Lowercase, replace invalid characters, collapse separators, strip edges (the runtime's own rule)."""
    if not value:
        raise ConsumerGroupDerivationError(
            "consumer-group identifier component is empty"
        )
    result = _INVALID.sub("_", value.lower())
    result = _SEPARATORS.sub(lambda m: m.group(0)[0], result)
    result = _EDGES.sub("", result)
    if not result:
        raise ConsumerGroupDerivationError(
            f"identifier component {value!r} normalizes to an empty string"
        )
    if len(result) > _GROUP_MAX:
        suffix = hashlib.sha256(value.encode()).hexdigest()[:8]
        result = f"{result[: _GROUP_MAX - 9]}_{suffix}"
    return result


def _base_group_id(*, env: str, package: str, node: str, version: str) -> str:
    parts = [
        normalize_kafka_identifier(p) for p in (env, package, node, _PURPOSE, version)
    ]
    group_id = ".".join(parts)
    if len(group_id) > _GROUP_MAX:
        digest = hashlib.sha256(
            f"{env}|{package}|{node}|{_PURPOSE}|{version}".encode()
        ).hexdigest()[:8]
        group_id = f"{group_id[: _GROUP_MAX - 9]}_{digest}"
    return group_id


def _version(value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, Mapping):
        try:
            return f"{value['major']}.{value['minor']}.{value['patch']}"
        except KeyError as exc:
            raise ConsumerGroupDerivationError(
                f"contract_version {value!r} is missing {exc}"
            ) from exc
    raise ConsumerGroupDerivationError(
        f"unrecognised contract_version payload: {value!r}"
    )


def derive_groups(
    manifest: Mapping[str, Any],
    *,
    env: str,
    instance: str,
    nodes: Sequence[tuple[str, str]],
) -> tuple[str, ...]:
    """The live group name of every subscription of every declared node, sorted.

    Contracts with no subscribe topics, or with ``event_bus.plugin_managed`` (a plugin owns that
    subscription), mint no group. Every declared node must yield at least one group.
    """
    contracts = manifest.get("contracts")
    if not isinstance(contracts, list):
        raise ConsumerGroupDerivationError(
            "the manifest has no contracts list; refusing to derive groups from an unrecognised payload"
        )
    instance_part = normalize_kafka_identifier(instance)
    wanted = {(package, node) for package, node in nodes}
    groups: dict[tuple[str, str], list[str]] = {key: [] for key in wanted}
    for contract in contracts:
        if not isinstance(contract, Mapping):
            continue
        key = (contract.get("package_name"), contract.get("name"))
        if key not in wanted:
            continue
        event_bus = contract.get("event_bus")
        if not isinstance(event_bus, Mapping) or event_bus.get("plugin_managed"):
            continue
        topics = event_bus.get("subscribe_topics")
        if not isinstance(topics, list):
            continue
        base = _base_group_id(
            env=env,
            package=str(key[0]),
            node=str(key[1]),
            version=_version(contract.get("contract_version")),
        )
        groups[key].extend(
            f"{base}{_INSTANCE_INFIX}{instance_part}{_TOPIC_INFIX}{topic}"
            for topic in topics
            if isinstance(topic, str) and topic
        )
    missing = sorted(f"{p}/{n}" for (p, n), found in groups.items() if not found)
    if missing:
        raise ConsumerGroupDerivationError(
            f"the manifest carries no subscribing contract for {', '.join(missing)}; "
            "the declared node is not running or no longer subscribes"
        )
    return tuple(sorted({g for found in groups.values() for g in found}))


def _declared_contracts(
    manifest: Mapping[str, Any], nodes: Sequence[tuple[str, str]]
) -> dict[str, Any]:
    """Only the declared nodes' contracts: what the fallback needs, a few kilobytes of a 2 MB manifest."""
    wanted = set(nodes)
    contracts = manifest.get("contracts")
    kept = (
        [
            c
            for c in contracts
            if isinstance(c, Mapping)
            and (c.get("package_name"), c.get("name")) in wanted
        ]
        if isinstance(contracts, list)
        else []
    )
    return {"contracts": kept}


class HandlerBrokerLag:
    """Read the declared nodes' live consumer groups over the injected manifest source and lag reader."""

    def __init__(
        self,
        *,
        overlay: ModelLabAlarmOverlay,
        manifest: ProtocolManifestSource,
        lag: ProtocolGroupLagReader,
        samples: ProtocolSampleStore,
        records: ProtocolRunRecordStore,
        clock: Callable[[], datetime],
        manifest_cache: ProtocolManifestCache | None = None,
        streaks: ProtocolGrowthStreakStore | None = None,
        delivery: ProtocolAlarmDelivery | None = None,
    ) -> None:
        if overlay.lag.growth is not None and streaks is None:
            raise ValueError(
                "the overlay declares lag.growth, so a growth streak store is required"
            )
        self._streaks = streaks
        self._delivery = delivery
        self._manifest_cache = manifest_cache
        self._overlay = overlay
        self._manifest = manifest
        self._lag = lag
        self._samples = samples
        self._records = records
        self._clock = clock

    def handle(self, request: ModelBrokerLagCommand) -> ModelBrokerLagResult:
        drill = request.fault is not EnumLabAlarmFault.NONE
        previous = self._samples.load()
        nodes = [(n.package, n.node) for n in self._overlay.groups.nodes]
        if request.fault is EnumLabAlarmFault.UNKNOWN_NODE:
            nodes.append(_UNKNOWN_NODE)
        try:
            manifest, notes = self._manifest_for(request, nodes)
        except ManifestUnreachableError as exc:
            return self._unreachable(request, str(exc), previous)
        except ManifestUnreadableError as exc:
            return self._indeterminate(request, (), (f"derivation:{exc}",), previous)
        try:
            groups = derive_groups(
                manifest,
                env=self._overlay.groups.env,
                instance=self._overlay.groups.instance,
                nodes=nodes,
            )
        except ConsumerGroupDerivationError as exc:
            return self._indeterminate(request, (), (f"derivation:{exc}",), previous)
        readings, sample, unreadable = self._read_all(groups, previous, request.fault)
        return self._judge(
            request, groups, readings, sample, unreadable, previous, drill, notes
        )

    def _manifest_for(
        self, request: ModelBrokerLagCommand, nodes: Sequence[tuple[str, str]]
    ) -> tuple[Mapping[str, Any], tuple[str, ...]]:
        """The manifest to derive from, and the notes naming a fallback.

        A reachable manifest is kept as the last good one (only the declared nodes' contracts). An
        unreachable one falls back to that copy while it is within the overlay's staleness bound, and the
        note names its age; with no copy, or an older one, the unreachable error stands. A manifest that
        arrived and cannot be read never falls back: that is not an endpoint that is down.
        """
        try:
            if request.fault is EnumLabAlarmFault.MANIFEST_DOWN:
                raise ManifestUnreachableError(
                    "manifest fetch failed (injected manifest-down)"
                )
            manifest = self._manifest.read()
        except ManifestUnreachableError as exc:
            return self._last_good(str(exc))
        if (
            self._manifest_cache is not None
            and request.write
            and request.fault is EnumLabAlarmFault.NONE
        ):
            self._manifest_cache.save(
                _declared_contracts(manifest, nodes), self._clock()
            )
        return manifest, ()

    def _last_good(self, why: str) -> tuple[Mapping[str, Any], tuple[str, ...]]:
        resilience = self._overlay.manifest.resilience
        if self._manifest_cache is None or resilience is None:
            raise ManifestUnreachableError(why)
        cached = self._manifest_cache.load()
        if cached is None:
            raise ManifestUnreachableError(f"{why}; no last good manifest is kept")
        manifest, fetched_at = cached
        age = max(0, int((self._clock() - fetched_at).total_seconds()))
        if age > resilience.max_stale_seconds:
            raise ManifestUnreachableError(
                f"{why}; the last good manifest is {age}s old, over the {resilience.max_stale_seconds}s bound"
            )
        return manifest, (
            f"manifest-stale:group names from the last good manifest, {age}s old ({why})",
        )

    def _read_all(
        self,
        groups: Sequence[str],
        previous: Mapping[str, int] | None,
        fault: EnumLabAlarmFault,
    ) -> tuple[list[ModelGroupReading], dict[str, int], list[str]]:
        readings: list[ModelGroupReading] = []
        sample: dict[str, int] = {}
        unreadable: list[str] = []
        for index, group in enumerate(groups):
            before = None if previous is None else previous.get(group)
            try:
                lag = self._lag.total_lag(group)
            except GroupNeverCommittedError as exc:
                readings.append(
                    ModelGroupReading(
                        group=group,
                        state=EnumGroupState.NEVER_COMMITTED,
                        previous_lag=before,
                        detail=str(exc),
                    )
                )
                continue
            except GroupUnreadableError as exc:
                unreadable.append(f"{group} ({exc})")
                readings.append(
                    ModelGroupReading(
                        group=group,
                        state=EnumGroupState.UNREADABLE,
                        previous_lag=before,
                        detail=str(exc),
                    )
                )
                continue
            if fault is EnumLabAlarmFault.PLANTED_LAG and index == 0:
                lag = (before or 0) + _PLANTED_GROWTH
            sample[group] = lag
            readings.append(
                ModelGroupReading(
                    group=group,
                    state=EnumGroupState.READ,
                    lag=lag,
                    previous_lag=before,
                )
            )
        return readings, sample, unreadable

    def _judge(
        self,
        request: ModelBrokerLagCommand,
        groups: Sequence[str],
        readings: list[ModelGroupReading],
        sample: dict[str, int],
        unreadable: list[str],
        previous: dict[str, int] | None,
        drill: bool,
        notes: tuple[str, ...] = (),
    ) -> ModelBrokerLagResult:
        if unreadable:
            return self._result(
                request,
                groups,
                readings,
                (),
                tuple(unreadable),
                EnumBrokerLagOutcome.INDETERMINATE,
                sample,
                previous,
                drill,
                "an unreadable group is not a group at zero",
                notes,
            )
        never = tuple(
            f"{r.group}: {r.detail}"
            for r in readings
            if r.state is EnumGroupState.NEVER_COMMITTED
        )
        if previous is None:
            return self._result(
                request,
                groups,
                readings,
                (),
                ("no-previous-sample", *never),
                EnumBrokerLagOutcome.INDETERMINATE,
                sample,
                previous,
                drill,
                "no previous sample, so growth could not be evaluated; this run is the baseline",
                notes,
            )
        alarms: list[str] = []
        waiting: list[str] = []
        rule = self._overlay.lag.growth
        stored = self._streaks.load() if rule is not None and self._streaks else {}
        streaks: dict[str, int] = {}
        for index, reading in enumerate(readings):
            before = reading.previous_lag
            if reading.state is EnumGroupState.READ and before is not None:
                if rule is None:
                    if reading.lag is not None and reading.lag > before:
                        alarms.append(
                            f"{reading.group}: lag grew {before}->{reading.lag}"
                        )
                elif (
                    reading.lag is not None and reading.lag - before >= rule.min_growth
                ):
                    run = stored.get(reading.group, 0) + 1
                    streaks[reading.group] = run
                    planted = (
                        request.fault is EnumLabAlarmFault.PLANTED_LAG and index == 0
                    )
                    if run >= rule.persist_runs or planted:
                        alarms.append(
                            f"{reading.group}: lag grew {before}->{reading.lag} for {run} samples in a row"
                        )
                    else:
                        waiting.append(
                            f"{reading.group}: lag grew {before}->{reading.lag}, run {run} of {rule.persist_runs}"
                        )
            elif reading.state is EnumGroupState.NEVER_COMMITTED and before is not None:
                alarms.append(
                    f"{reading.group}: held {before}, now never-committed (offsets vanished)"
                )
        outcome = EnumBrokerLagOutcome.ALARM if alarms else EnumBrokerLagOutcome.OK
        return self._result(
            request,
            groups,
            readings,
            tuple(alarms),
            (*never, *waiting),
            outcome,
            sample,
            previous,
            drill,
            "growth across two consecutive samples, and only growth; a high flat lag is not an alarm"
            if rule is None
            else f"growth of {rule.min_growth} or more in {rule.persist_runs} consecutive samples, and only that; a high flat lag is not an alarm",
            notes,
            streaks,
        )

    def _indeterminate(
        self,
        request: ModelBrokerLagCommand,
        groups: tuple[str, ...],
        reasons: tuple[str, ...],
        previous: dict[str, int] | None,
    ) -> ModelBrokerLagResult:
        # The previous sample is left as it was: the next tick that can derive compares against it.
        result = ModelBrokerLagResult(
            correlation_id=request.correlation_id,
            outcome=EnumBrokerLagOutcome.INDETERMINATE,
            fault=request.fault,
            declared_groups=groups,
            readings=(),
            alarms=(),
            reasons=reasons,
            sample_kept=False,
            evidence="the watched groups could not be derived from the running image's contracts, so no lag was sampled; an alarm that cannot name its subjects has not read zero lag",
            failure="indeterminate:" + ";".join(reasons),
        )
        return self._record(request, result)

    def _unreachable(
        self,
        request: ModelBrokerLagCommand,
        why: str,
        previous: dict[str, int] | None,
    ) -> ModelBrokerLagResult:
        """The manifest endpoint did not answer and no usable last good copy exists.

        Still INDETERMINATE, never OK and never muted, but named for what it is: no lag was read, and
        nothing here says the lag is high or low. The previous sample is left as it was.
        """
        reasons = (f"manifest-unreachable:{why}; lag was not read",)
        result = ModelBrokerLagResult(
            correlation_id=request.correlation_id,
            outcome=EnumBrokerLagOutcome.INDETERMINATE,
            fault=request.fault,
            declared_groups=(),
            readings=(),
            alarms=(),
            reasons=reasons,
            sample_kept=False,
            evidence="the manifest endpoint did not answer, so the group names and the lag were not read; this is the reader's reading, not a lag figure",
            failure="indeterminate:" + ";".join(reasons),
        )
        return self._record(request, result)

    def _record(
        self, request: ModelBrokerLagCommand, result: ModelBrokerLagResult
    ) -> ModelBrokerLagResult:
        """Append the finished run to the record the floor reads; a drill never writes to it.

        The floor draws its broker lag source from the last line of this record, so a run that reads
        INDETERMINATE or ALARM is on the board the same as an OK one. A record that cannot be written
        is a typed failure of the run, never a silent skip: the floor would otherwise go on reading an
        old line as if it were this run.
        """
        if not request.write or request.fault is not EnumLabAlarmFault.NONE:
            return result
        # What the floor shows of a run that is not OK is the first 110 characters of this line, so it leads
        # with the alarms or the reasons; an OK run says what was read.
        said = (
            result.evidence
            if result.outcome is EnumBrokerLagOutcome.OK
            else "; ".join(result.alarms or result.reasons) or result.evidence
        )
        try:
            self._records.append(
                {
                    "finished_at": self._clock().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "reports": [
                        {
                            "condition": self._overlay.lag.record_condition,
                            "outcome": result.outcome.value,
                            "evidence": said,
                        }
                    ],
                }
            )
        except RunRecordUnwritableError as exc:
            return result.model_copy(update={"failure": f"record-unwritable:{exc}"})
        return result

    def _result(
        self,
        request: ModelBrokerLagCommand,
        groups: Sequence[str],
        readings: list[ModelGroupReading],
        alarms: tuple[str, ...],
        reasons: tuple[str, ...],
        outcome: EnumBrokerLagOutcome,
        sample: dict[str, int],
        previous: dict[str, int] | None,
        drill: bool,
        rule: str,
        notes: tuple[str, ...] = (),
        streaks: Mapping[str, int] | None = None,
    ) -> ModelBrokerLagResult:
        # A drill never moves the baseline, and a pass that could not read every group keeps none.
        keep = (
            request.write
            and not drill
            and not any(r.state is EnumGroupState.UNREADABLE for r in readings)
        )
        if keep:
            self._samples.save(sample)
            if self._streaks is not None:
                self._streaks.save(streaks or {})
        failure = ""
        if outcome is EnumBrokerLagOutcome.ALARM:
            failure = "alarm:" + ";".join(alarms)
        elif outcome is EnumBrokerLagOutcome.INDETERMINATE:
            failure = "indeterminate:" + ";".join((*notes, *reasons))
        result = ModelBrokerLagResult(
            correlation_id=request.correlation_id,
            outcome=outcome,
            fault=request.fault,
            declared_groups=tuple(groups),
            readings=tuple(readings),
            alarms=alarms,
            reasons=(*notes, *reasons),
            sample_kept=keep,
            evidence=(f"{notes[0]}; " if notes else "")
            + f"{len(groups)} groups derived, {len(sample)} read; {rule}",
            failure=failure,
        )
        return self._record(request, self._deliver(request, result))

    def _deliver(
        self, request: ModelBrokerLagCommand, result: ModelBrokerLagResult
    ) -> ModelBrokerLagResult:
        """Hand the verdict to the alarm delivery; a probe and a drill never deliver.

        A delivery that fails or cannot run is named in the result's failure, so the run ends as an alarm
        meanwhile: an alarm that believes it was delivered and was not is the defect the delivery exists for.
        """
        if (
            self._delivery is None
            or not request.write
            or request.fault is not EnumLabAlarmFault.NONE
        ):
            return result
        condition = self._overlay.lag.record_condition
        try:
            reply = self._delivery.handle(
                ModelLabAlarmDeliveryRequest(
                    correlation_id=request.correlation_id,
                    condition=condition,
                    outcome=EnumDeliveryCondition(result.outcome.value),
                    alarms=typed_alarms(result.alarms, condition),
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
        result: ModelBrokerLagResult, entries: tuple[str, ...], failure: str
    ) -> ModelBrokerLagResult:
        return result.model_copy(
            update={
                "delivery": entries,
                "failure": f"{result.failure}; {failure}"
                if result.failure
                else failure,
            }
        )


def typed_alarms(alarms: Sequence[str], condition: str) -> tuple[ModelLabAlarm, ...]:
    """Each alarm line is ``<group>: <what happened>``; a group name never holds ``: ``, so the first one splits it."""
    typed: list[ModelLabAlarm] = []
    for line in alarms:
        subject, separator, detail = line.partition(": ")
        if not separator:
            raise ValueError(f"alarm line {line!r} names no subject before ': '")
        typed.append(ModelLabAlarm(condition=condition, subject=subject, detail=detail))
    return tuple(typed)


def events_for(
    result: ModelBrokerLagResult, topics: Mapping[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The chain of one run: started, one reading per group, then the completed or the failed terminal."""
    cid = result.correlation_id
    chain: list[tuple[str, dict[str, object]]] = [
        (
            topics["broker_lag_run_started"],
            {"correlation_id": cid, "declared": len(result.declared_groups)},
        )
    ]
    chain.extend(
        (
            topics["broker_lag_group_read"],
            {
                "correlation_id": cid,
                "group": r.group,
                "state": r.state.value,
                "lag": r.lag,
                "previous_lag": r.previous_lag,
            },
        )
        for r in result.readings
    )
    final = topics[
        "broker_lag_run_completed"
        if result.outcome is EnumBrokerLagOutcome.OK and not result.failure
        else "broker_lag_run_failed"
    ]
    chain.append(
        (
            final,
            {
                "correlation_id": cid,
                "outcome": result.outcome.value,
                "fault": result.fault.value,
                "alarms": list(result.alarms),
                "reasons": list(result.reasons),
                "sample_kept": result.sample_kept,
                "failure": result.failure,
            },
        )
    )
    return chain


def parse_command(body: bytes) -> ModelBrokerLagCommand | str:
    """The command in a request body, or the bad-payload failure naming why."""
    try:
        return ModelBrokerLagCommand.model_validate_json(body)
    except ValidationError as exc:
        return "bad-payload:" + ",".join(
            ".".join(str(p) for p in e["loc"]) or "body" for e in exc.errors()
        )


__all__ = [
    "ConsumerGroupDerivationError",
    "HandlerBrokerLag",
    "derive_groups",
    "events_for",
    "load_topics",
    "normalize_kafka_identifier",
    "parse_command",
    "typed_alarms",
]
