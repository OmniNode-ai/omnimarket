# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A failed delegation scenario run writes one ledger alarm row (OMN-24874).

Each watched scenario's unit writes a last-run record (``{"conclusion", "ran_at", "scenario"}``) under the home directory.
One tick reads every record the contract's ``alarm`` block names. A failed, missing or unreadable record yields one STATUS row with ``src=alarm`` and ``alarm=<the scenario's alarm
name>``, the unit, the run's time and an owner line. Fresh skips leave the last success time
intact. Starting at the third consecutive skip, a separate dev readiness alarm identifies the run. A passing record yields no row unless it is older than the scenario's declared
``max_age_seconds`` (or a passing record's time is unreadable): a unit killed before it wrote its record, or a stopped timer, leaves the last
run's record in place, and that reads ``stale``, whether the last run succeeded, failed or was refused. The request id is the
scenario, the host, the run's own time, the conclusion and the alarm time bucket only for missing, unreadable or stale records.
A failed run with a readable time that is still inside ``max_age_seconds`` is keyed on that time alone and stamped with it, so
ticks on either side of a bucket boundary submit the identical row and request id. A missing record uses the bucket start,
allowing another outage reading in each bucket; a stale record is stamped at the later of the bucket start and the moment it
went stale (its ``ran_at`` plus ``max_age_seconds``), so a reader window that starts at the run it cites finds the row. A run
time ahead of the tick, and a fresh record that concludes the literal ``stale``, read as bucketed readings stamped at the
bucket start. No stamp depends on the tick's own second: a second tick submits the identical row. The pattern is the deploy-job-failed alarm row of omnibase_infra's
deploy job reading.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_delegation_scenario import EnumScenarioConclusion
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)

NODE_DIR = Path(__file__).resolve().parents[1]
CONTRACT_PATH = NODE_DIR / "contract.yaml"

Clock = Callable[[], datetime]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelScenarioWatch(_Frozen):
    """One watched scenario: its id, the alarm name its row carries, its unit and its last-run record."""

    scenario: str = Field(min_length=1)
    alarm: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    not_ready_alarm: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    unit: str = Field(min_length=1)
    evidence_file: str = Field(min_length=1)
    max_age_seconds: int = Field(gt=0)


class ModelScenarioAlarmSettings(_Frozen):
    """The contract's ``alarm`` block."""

    lane: str = Field(min_length=1)
    ticket: str = Field(pattern=r"^OMN-\d+$")
    actor: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    home_env: str = Field(min_length=1)
    host_env: str = Field(min_length=1)
    ledger_append_command_env: str = Field(min_length=1)
    ledger_append_timeout_seconds: int = Field(gt=0)
    success_conclusion: str = Field(min_length=1)
    alarm_bucket_seconds: int = Field(gt=0)
    scenarios: tuple[ModelScenarioWatch, ...] = Field(min_length=1)


class ModelScenarioAlarmRequest(_Frozen):
    """One alarm tick. ``home`` replaces the home directory the records are read under (a sandbox run)."""

    home: Path
    host: str = Field(min_length=1)


class EnumScenarioAlarm(StrEnum):
    FAILURE = "failure"
    DEV_NOT_READY = "dev-not-ready"


class ModelScenarioReading(_Frozen):
    """What one tick read for one scenario, and the alarm row it appended, if any."""

    scenario: str
    conclusion: str
    ran_at: str
    last_success_at: str | None = None
    alarm_kind: EnumScenarioAlarm | None = None
    alarm_row: str | None = None
    append_status: str | None = None


class ModelScenarioAlarmResult(_Frozen):
    """The readings of one tick, one per watched scenario."""

    readings: tuple[ModelScenarioReading, ...]

    @property
    def alarms(self) -> tuple[ModelScenarioReading, ...]:
        return tuple(r for r in self.readings if r.alarm_row is not None)

    @property
    def unappended(self) -> tuple[ModelScenarioReading, ...]:
        return tuple(
            r for r in self.alarms if r.append_status not in ("accepted", "duplicate")
        )


def load_alarm_settings(path: Path = CONTRACT_PATH) -> ModelScenarioAlarmSettings:
    """The contract's ``alarm`` block, typed."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ModelScenarioAlarmSettings.model_validate(document["alarm"])


def _clean(value: object) -> str:
    """One ledger cell value: no cell separator, no line break, never empty."""
    text = " ".join(str(value).split()).replace("|", "/")
    return text or "unknown"


@dataclass(frozen=True)
class _LastRun:
    conclusion: str = "no-record"
    ran_at: str = "unknown"
    last_success_at: str | None = None
    consecutive_skips: int = 0


def _read_last_run(path: Path) -> _LastRun:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _LastRun()
    if not isinstance(document, dict):
        return _LastRun()
    conclusion = document.get("conclusion")
    ran_at = document.get("ran_at")
    last_success_at = document.get("last_success_at")
    if conclusion == EnumScenarioConclusion.SUCCESS and not last_success_at:
        last_success_at = ran_at
    skip_count = document.get("consecutive_skips")
    return _LastRun(
        conclusion=conclusion
        if isinstance(conclusion, str) and conclusion
        else "no-record",
        ran_at=_clean(ran_at) if ran_at else "unknown",
        last_success_at=last_success_at if isinstance(last_success_at, str) else None,
        consecutive_skips=skip_count
        if type(skip_count) is int and skip_count > 0
        else 0,
    )


def read_last_run(path: Path) -> tuple[str, str]:
    """The last recorded conclusion and run time."""
    record = _read_last_run(path)
    return record.conclusion, record.ran_at


def alarm_bucket(settings: ModelScenarioAlarmSettings, now: datetime) -> int:
    """The index of the alarm time bucket ``now`` falls in."""
    return int(now.timestamp()) // settings.alarm_bucket_seconds


def alarm_request_id(
    watch: ModelScenarioWatch,
    host: str,
    ran_at: str,
    bucket: int | None,
    conclusion: str,
    kind: EnumScenarioAlarm = EnumScenarioAlarm.FAILURE,
) -> str:
    """One UUID per readable run; missing, unreadable and stale readings also name their conclusion and bucket.

    A bucketed row differs in its bytes by conclusion, and the ledger answers a repeated request id duplicate only for
    identical bytes, so two readings of one bucket that differ in conclusion must not share an id.
    """
    alarm = (
        watch.not_ready_alarm
        if kind is EnumScenarioAlarm.DEV_NOT_READY
        else watch.alarm
    )
    key = f"onex-scenario-alarm:{alarm}|{host}|{ran_at}"
    if bucket is not None:
        key += f"|{conclusion}|{bucket}"
    return str(uuid5(NAMESPACE_URL, key))


def _read_run_time(ran_at: str) -> datetime | None:
    """Read an aware run time; an absent time zone cannot identify a run."""
    try:
        ran = datetime.fromisoformat(ran_at)
    except ValueError:
        return None
    return ran if ran.tzinfo is not None else None


def is_stale(ran_at: str, max_age_seconds: int, now: datetime) -> bool:
    """True when ``ran_at`` is unreadable or more than ``max_age_seconds`` before ``now``."""
    ran = _read_run_time(ran_at)
    return ran is None or (now - ran).total_seconds() > max_age_seconds


def build_alarm_row(
    settings: ModelScenarioAlarmSettings,
    watch: ModelScenarioWatch,
    *,
    host: str,
    conclusion: str,
    ran_at: str,
    bucket: int | None,
    last_conclusion: str | None = None,
    kind: EnumScenarioAlarm = EnumScenarioAlarm.FAILURE,
) -> str:
    """Stamp a failed run at its own time, or a recurring outage at its bucket start.

    The stamp is a function of the record and the bucket only, never of the tick that writes the row: the request id is
    fixed for one record and one bucket, and the ledger answers a repeated id duplicate only for identical bytes, so a
    stamp tied to the tick's own second would turn the second tick's row into a conflict.

    ``last_conclusion`` is the conclusion of the record a stale reading replaced; it is set only for a record the handler
    read as older than its declared age, which is the only record whose ``ran_at`` plus ``max_age_seconds`` is behind
    the tick.
    """
    if bucket is None:
        moment = _read_run_time(ran_at)
        if moment is None:
            raise ValueError("a run alarm requires a readable ran_at")
    else:
        moment = datetime.fromtimestamp(bucket * settings.alarm_bucket_seconds, UTC)
        # A stale reading cites a run that went stale at ran_at + max_age. The bucket start is usually before that
        # run (R9's max_age is far inside the bucket), and a reader window that starts at ran_at would miss a row
        # stamped before it, so the stamp is the later of the two. Both are fixed for one record and one bucket.
        # A fresh record that concludes the literal ``stale`` has no such moment (it is not older than its age), so it
        # is stamped at the bucket start like any other bucketed reading.
        ran = _read_run_time(ran_at)
        if conclusion == "stale" and last_conclusion is not None and ran is not None:
            moment = max(moment, ran + timedelta(seconds=watch.max_age_seconds))
    # Ledger stamps have second precision. Round a fractional run time up so a
    # reader whose window starts at ran_at cannot exclude this run's alarm.
    if moment.microsecond:
        moment = moment.replace(microsecond=0) + timedelta(seconds=1)
    stamp = moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if kind is EnumScenarioAlarm.DEV_NOT_READY:
        reason = "dev readiness was unavailable for at least three consecutive runs"
    elif conclusion == "no-record":
        reason = "no readable last-run record"
    elif conclusion == "stale":
        reason = f"the last run is older than {watch.max_age_seconds} s or has no readable time"
        if last_conclusion is not None:
            reason += f" (it concluded {last_conclusion})"
    else:
        reason = f"the run concluded {conclusion}"
    cells = [
        stamp,
        "STATUS",
        f"lane={settings.lane}",
        f"ticket={settings.ticket}",
        f"actor={settings.actor}",
        "model=none",
        "src=alarm",
        f"alarm={watch.not_ready_alarm if kind is EnumScenarioAlarm.DEV_NOT_READY else watch.alarm}",
        f"scenario={_clean(watch.scenario)}",
        f"conclusion={_clean(conclusion)}",
        f"ran_at={_clean(ran_at)}",
        f"host={_clean(host)}",
        f"unit={_clean(watch.unit)}",
        f"owner={_clean(settings.owner)}",
        _clean(
            f"scenario {watch.scenario} on {host}: {reason} (ran_at {ran_at}); "
            f"read journalctl --user -u {watch.unit} and its last-run record"
        ),
    ]
    return " | ".join(cells)


class HandlerDelegationScenarioAlarm:
    """Read each watched scenario's last-run record and append one alarm row per failed run."""

    def __init__(
        self,
        settings: ModelScenarioAlarmSettings,
        appender: ProtocolReadingAppender,
        *,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._appender = appender
        self._clock = clock

    def handle(self, request: ModelScenarioAlarmRequest) -> ModelScenarioAlarmResult:
        readings: list[ModelScenarioReading] = []
        now = self._clock()
        bucket = alarm_bucket(self._settings, now)
        for watch in self._settings.scenarios:
            record = _read_last_run(request.home / watch.evidence_file)
            conclusion, ran_at = record.conclusion, record.ran_at
            last_conclusion: str | None = None
            # A record older than the declared age reads stale whatever it concluded: a failed or refused run whose
            # unit then stops would otherwise stay keyed on its ran_at, one row and then duplicates, never an alarm.
            # A non-success record with no readable time stays its own bucketed reading (its conclusion is the news).
            if (
                conclusion == self._settings.success_conclusion
                or (conclusion != "no-record" and _read_run_time(ran_at) is not None)
            ) and is_stale(ran_at, watch.max_age_seconds, now):
                last_conclusion = conclusion
                conclusion = "stale"
            skipped = conclusion == EnumScenarioConclusion.SKIPPED
            if conclusion == self._settings.success_conclusion or (
                skipped and record.consecutive_skips < 3
            ):
                readings.append(
                    ModelScenarioReading(
                        scenario=watch.scenario,
                        conclusion=conclusion,
                        ran_at=ran_at,
                        last_success_at=record.last_success_at,
                    )
                )
                continue
            # A failed run remains the same run across later ticks. Only missing,
            # unreadable and stale records represent recurring outage readings.
            # A run time ahead of the tick (clock skew, a hand-written record) cannot identify a run either: it reads
            # as unreadable, so its row is bucketed and stamped at the bucket start, never at the tick.
            run_time = _read_run_time(ran_at)
            row_bucket = (
                bucket
                if conclusion in ("no-record", "stale")
                or run_time is None
                or run_time > now
                else None
            )
            kind = (
                EnumScenarioAlarm.DEV_NOT_READY
                if skipped
                else EnumScenarioAlarm.FAILURE
            )
            row = build_alarm_row(
                self._settings,
                watch,
                host=request.host,
                conclusion=conclusion,
                ran_at=ran_at,
                bucket=row_bucket,
                last_conclusion=last_conclusion,
                kind=kind,
            )
            outcome = self._appender.append(
                row,
                alarm_request_id(
                    watch, request.host, ran_at, row_bucket, conclusion, kind
                ),
            )
            readings.append(
                ModelScenarioReading(
                    scenario=watch.scenario,
                    conclusion=conclusion,
                    ran_at=ran_at,
                    last_success_at=record.last_success_at,
                    alarm_kind=kind,
                    alarm_row=row,
                    append_status=outcome.status,
                )
            )
        return ModelScenarioAlarmResult(readings=tuple(readings))


__all__ = [
    "EnumScenarioAlarm",
    "HandlerDelegationScenarioAlarm",
    "ModelScenarioAlarmRequest",
    "ModelScenarioAlarmResult",
    "ModelScenarioAlarmSettings",
    "ModelScenarioReading",
    "ModelScenarioWatch",
    "alarm_bucket",
    "alarm_request_id",
    "build_alarm_row",
    "is_stale",
    "load_alarm_settings",
    "read_last_run",
]
