# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerHostDiskDecision: what each host evicts and when it alarms, as a pure function (OMN-23866).

Definition-B compute: ``handle(request: ModelHostDiskDecisionRequest) -> ModelHostDiskDecision``. No clock
(``now`` is an input), no I/O, no value of policy in this file.

Rules, each tied to a reason the command names:

* ``age_past_keep_days``: an entry group whose newest use is older than the class's keep window and that no live
  lane holds.
* ``host_under_floor``: while the host's free space on its policy volume is under its floor, the class's shorter
  ``pressure_keep_days`` window applies instead.
* ``class_over_cap``: a class larger than its cap, after the above, loses its oldest unheld entry groups until it is
  under the cap; a class with no per-entry age (docker) is pruned to the cap by the effect.

The newest revision of every package is never a target, and a group any lane holds is never a target. A stale
reading decides no command: it raises ``stale_reading`` instead, as a missing one raises ``missing_reading``.
"""

from __future__ import annotations

from omnimarket.models.model_host_disk_decision import (
    REASON_PRIORITY,
    SECONDS_PER_DAY,
    EnumHostDiskAlarmKind,
    EnumHostDiskReason,
    ModelHostDiskAlarm,
    ModelHostDiskClassPolicy,
    ModelHostDiskCommand,
    ModelHostDiskDecision,
    ModelHostDiskDecisionRequest,
    ModelHostDiskEvictTarget,
    ModelHostDiskHostReading,
    ModelHostDiskPolicy,
)
from omnimarket.models.model_host_disk_reading import (
    ALL_CLASSES,
    DiskClassName,
    ModelDiskClassReading,
    ModelDiskGroup,
)


def decide_host_disk(request: ModelHostDiskDecisionRequest) -> ModelHostDiskDecision:
    """Decide, per host, the eviction commands and the alarms."""
    policy = request.policy
    readings = {r.host_name: r for r in request.readings}
    commands: list[ModelHostDiskCommand] = []
    alarms: list[ModelHostDiskAlarm] = []
    for host in sorted(set(readings) | set(policy.hosts)):
        reading = readings.get(host)
        if reading is None:
            alarms.append(
                ModelHostDiskAlarm(
                    host_name=host,
                    kind=EnumHostDiskAlarmKind.MISSING_READING,
                    detail=f"no disk reading for host {host}",
                    floor_free_bytes=policy.floor_for(host),
                    idempotency_key=f"{host}|{EnumHostDiskAlarmKind.MISSING_READING}|none",
                )
            )
            continue
        age = int((request.now - reading.read_at).total_seconds())
        if age < 0 or age > policy.max_reading_age_seconds:
            alarms.append(
                ModelHostDiskAlarm(
                    host_name=host,
                    kind=EnumHostDiskAlarmKind.STALE_READING,
                    detail=(
                        f"reading of host {host} is {age}s old; "
                        f"the policy allows {policy.max_reading_age_seconds}s"
                    ),
                    floor_free_bytes=policy.floor_for(host),
                    reading_age_seconds=age,
                    reading_read_at=reading.read_at,
                    idempotency_key=(
                        f"{host}|{EnumHostDiskAlarmKind.STALE_READING}|{reading.read_at.isoformat()}"
                    ),
                )
            )
            continue
        floor = policy.floor_for(host)
        volume_path = policy.volume_for(host)
        volume = next((v for v in reading.volumes if v.path == volume_path), None)
        under_floor = False
        if volume is None:
            alarms.append(
                ModelHostDiskAlarm(
                    host_name=host,
                    kind=EnumHostDiskAlarmKind.MISSING_READING,
                    detail=f"volume {volume_path} of host {host} is not in its reading",
                    floor_free_bytes=floor,
                    reading_age_seconds=age,
                    reading_read_at=reading.read_at,
                    idempotency_key=(
                        f"{host}|{EnumHostDiskAlarmKind.MISSING_READING}|{reading.read_at.isoformat()}"
                    ),
                )
            )
        elif volume.free_bytes < floor:
            under_floor = True
            alarms.append(
                ModelHostDiskAlarm(
                    host_name=host,
                    kind=EnumHostDiskAlarmKind.UNDER_FLOOR,
                    detail=(
                        f"host {host} has {volume.free_bytes} bytes free on {volume_path}, "
                        f"under its floor of {floor}"
                    ),
                    free_bytes=volume.free_bytes,
                    floor_free_bytes=floor,
                    reading_age_seconds=age,
                    reading_read_at=reading.read_at,
                    idempotency_key=(
                        f"{host}|{EnumHostDiskAlarmKind.UNDER_FLOOR}|{reading.read_at.isoformat()}"
                    ),
                )
            )
        commands.extend(
            _class_commands(policy, reading, under_floor=under_floor),
        )
    return ModelHostDiskDecision(
        decided_at=request.now, commands=tuple(commands), alarms=tuple(alarms)
    )


def _class_commands(
    policy: ModelHostDiskPolicy,
    reading: ModelHostDiskHostReading,
    *,
    under_floor: bool,
) -> list[ModelHostDiskCommand]:
    by_class = {c.class_name: c for c in reading.classes}
    out: list[ModelHostDiskCommand] = []
    for class_name in ALL_CLASSES:
        class_reading = by_class.get(class_name)
        class_policy = policy.class_policy_for(reading.host_name, class_name)
        if (
            class_reading is None
            or class_policy is None
            or class_reading.status != "read"
        ):
            continue
        command = _decide_class(
            reading, class_name, class_reading, class_policy, under_floor=under_floor
        )
        if command is not None:
            out.append(command)
    return out


def _decide_class(
    reading: ModelHostDiskHostReading,
    class_name: DiskClassName,
    class_reading: ModelDiskClassReading,
    class_policy: ModelHostDiskClassPolicy,
    *,
    under_floor: bool,
) -> ModelHostDiskCommand | None:
    keep_days = class_policy.keep_days
    if under_floor and class_policy.pressure_keep_days is not None:
        keep_days = class_policy.pressure_keep_days
    keep_reason = (
        EnumHostDiskReason.HOST_UNDER_FLOOR
        if under_floor and class_policy.pressure_keep_days is not None
        else EnumHostDiskReason.AGE_PAST_KEEP_DAYS
    )
    protected = _newest_revision_of_each_package(class_reading.groups)
    # Candidates: aged, unheld, not the newest of their package. Oldest first, ties by name, so order is fixed.
    candidates = sorted(
        (
            g
            for g in class_reading.groups
            if g.newest_use_age_seconds is not None
            and g.held_copies == 0
            and (g.package, g.revision) not in protected
        ),
        key=lambda g: (-(g.newest_use_age_seconds or 0), g.package, g.revision),
    )
    targets: list[ModelHostDiskEvictTarget] = []
    chosen: set[tuple[str, str]] = set()
    if keep_days is not None:
        window = keep_days * SECONDS_PER_DAY
        for g in candidates:
            if (g.newest_use_age_seconds or 0) > window:
                targets.append(_target(g, keep_reason))
                chosen.add((g.package, g.revision))
    prune_to_bytes: int | None = None
    cap = class_policy.cap_bytes
    if cap is not None:
        remaining = class_reading.total_bytes - sum(t.bytes for t in targets)
        if remaining > cap:
            has_ages = any(
                g.newest_use_age_seconds is not None for g in class_reading.groups
            )
            if not has_ages:
                prune_to_bytes = cap
            for g in candidates:
                if remaining <= cap:
                    break
                if (g.package, g.revision) in chosen:
                    continue
                targets.append(_target(g, EnumHostDiskReason.CLASS_OVER_CAP))
                chosen.add((g.package, g.revision))
                remaining -= g.bytes
    if not targets and prune_to_bytes is None:
        return None
    targets.sort(key=lambda t: (t.package, t.revision))
    reasons = {t.reason for t in targets}
    if prune_to_bytes is not None:
        reasons.add(EnumHostDiskReason.CLASS_OVER_CAP)
    top = next(r for r in REASON_PRIORITY if r in reasons)
    return ModelHostDiskCommand(
        host_name=reading.host_name,
        class_name=class_name,
        reason=top,
        targets=tuple(targets),
        prune_to_bytes=prune_to_bytes,
        reading_read_at=reading.read_at,
        idempotency_key=f"{reading.host_name}|{class_name}|{reading.read_at.isoformat()}",
    )


def _newest_revision_of_each_package(
    groups: tuple[ModelDiskGroup, ...],
) -> set[tuple[str, str]]:
    """The (package, revision) of the most recently used group of every package, held or not."""
    newest: dict[str, ModelDiskGroup] = {}
    for g in groups:
        age = g.newest_use_age_seconds
        if age is None:
            continue
        current = newest.get(g.package)
        if current is None or (age, g.revision) < (
            current.newest_use_age_seconds or 0,
            current.revision,
        ):
            newest[g.package] = g
    return {(g.package, g.revision) for g in newest.values()}


def _target(
    group: ModelDiskGroup, reason: EnumHostDiskReason
) -> ModelHostDiskEvictTarget:
    return ModelHostDiskEvictTarget(
        package=group.package,
        revision=group.revision,
        bytes=group.bytes,
        reason=reason,
    )


class HandlerHostDiskDecision:
    """ONEX handler for the host disk decision compute."""

    def handle(self, request: ModelHostDiskDecisionRequest) -> ModelHostDiskDecision:
        return decide_host_disk(request)
