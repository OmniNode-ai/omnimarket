# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A failed delegation scenario run writes one ledger alarm row (OMN-24874).

Each watched scenario's unit writes a last-run record (``{"conclusion", "ran_at", "scenario"}``) under the home directory.
One tick reads every record the contract's ``alarm`` block names. A record whose conclusion is not the success conclusion,
and a record that is missing or unreadable, yields one STATUS row with ``src=alarm`` and ``alarm=<the scenario's alarm
name>``, the unit, the run's time and an owner line. A passing record yields no row unless it is older than the scenario's declared
``max_age_seconds`` (or its time is unreadable): a unit killed before it wrote its record, or a stopped timer, leaves the last
run's success in place, and that reads ``stale``. The request id is the scenario, the host, the run's own time and the alarm
time bucket (``alarm_bucket_seconds``), and the row is stamped with the bucket's start, not the tick's clock: the work ledger
answers a repeated request id duplicate only when the rows are identical, so a later tick in the same bucket over the same run
is answered duplicate, a new run or the next bucket is a new row, and a missing record in a later bucket is its own row. The
pattern is the deploy-job-failed alarm row of omnibase_infra's deploy job reading.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import yaml
from pydantic import BaseModel, ConfigDict, Field

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


class ModelScenarioReading(_Frozen):
    """What one tick read for one scenario, and the alarm row it appended, if any."""

    scenario: str
    conclusion: str
    ran_at: str
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


def read_last_run(path: Path) -> tuple[str, str]:
    """The record's ``(conclusion, ran_at)``; a missing or unreadable record reads ``("no-record", "unknown")``."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ("no-record", "unknown")
    if not isinstance(document, dict):
        return ("no-record", "unknown")
    conclusion = document.get("conclusion")
    ran_at = document.get("ran_at")
    if not isinstance(conclusion, str) or not conclusion:
        return ("no-record", _clean(ran_at) if ran_at else "unknown")
    return (conclusion, _clean(ran_at) if ran_at else "unknown")


def alarm_bucket(settings: ModelScenarioAlarmSettings, now: datetime) -> int:
    """The index of the alarm time bucket ``now`` falls in."""
    return int(now.timestamp()) // settings.alarm_bucket_seconds


def alarm_request_id(
    watch: ModelScenarioWatch, host: str, ran_at: str, bucket: int
) -> str:
    """One UUID per run and alarm bucket on one host.

    A retry or a later tick over the same run in the same bucket is a duplicate; a missing record in a later bucket
    is a new outage and gets its own id.
    """
    return str(
        uuid5(
            NAMESPACE_URL,
            f"onex-scenario-alarm:{watch.alarm}|{host}|{ran_at}|{bucket}",
        )
    )


def is_stale(ran_at: str, max_age_seconds: int, now: datetime) -> bool:
    """True when ``ran_at`` is unreadable or more than ``max_age_seconds`` before ``now``."""
    try:
        ran = datetime.fromisoformat(ran_at)
    except ValueError:
        return True
    if ran.tzinfo is None:
        return True
    return (now - ran).total_seconds() > max_age_seconds


def build_alarm_row(
    settings: ModelScenarioAlarmSettings,
    watch: ModelScenarioWatch,
    *,
    host: str,
    conclusion: str,
    ran_at: str,
    bucket: int,
) -> str:
    """The STATUS alarm row for one failed, stale or unreadable scenario run, stamped with its bucket's start."""
    stamp = datetime.fromtimestamp(
        bucket * settings.alarm_bucket_seconds, UTC
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    if conclusion == "no-record":
        reason = "no readable last-run record"
    elif conclusion == "stale":
        reason = f"the last run is older than {watch.max_age_seconds} s or has no readable time"
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
        f"alarm={watch.alarm}",
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
            conclusion, ran_at = read_last_run(request.home / watch.evidence_file)
            if conclusion == self._settings.success_conclusion and is_stale(
                ran_at, watch.max_age_seconds, now
            ):
                conclusion = "stale"
            if conclusion == self._settings.success_conclusion:
                readings.append(
                    ModelScenarioReading(
                        scenario=watch.scenario, conclusion=conclusion, ran_at=ran_at
                    )
                )
                continue
            row = build_alarm_row(
                self._settings,
                watch,
                host=request.host,
                conclusion=conclusion,
                ran_at=ran_at,
                bucket=bucket,
            )
            outcome = self._appender.append(
                row, alarm_request_id(watch, request.host, ran_at, bucket)
            )
            readings.append(
                ModelScenarioReading(
                    scenario=watch.scenario,
                    conclusion=conclusion,
                    ran_at=ran_at,
                    alarm_row=row,
                    append_status=outcome.status,
                )
            )
        return ModelScenarioAlarmResult(readings=tuple(readings))


__all__ = [
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
