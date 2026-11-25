# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the delegation check regression run.

One request is one change that can turn a delegation check red: a settings-store version change (the previous and the new
document), a host model-serving change, or a release. One result is what the run read for every check the change selected
and which of them turned red.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class EnumChangeKind(StrEnum):
    #: A new version of one overlay in the settings store; its changed keys are the diff of the two documents.
    SETTINGS_STORE = "settings_store"
    #: A model server on a lab host stopped, started or moved; the subject is the host.
    HOST_MODEL = "host_model"
    #: A package release of a repository the checks run against; the subject is the repository.
    RELEASE = "release"


class EnumCheckVerdict(StrEnum):
    GREEN = "green"
    RED = "red"
    #: The check was selected and no reading arrived inside its read deadline.
    UNREAD = "unread"


class EnumCheckRegressionOutcome(StrEnum):
    #: At least one selected check turned red; an alarm was raised for each.
    ALARM = "alarm"
    #: Checks were selected and none turned red.
    OK = "ok"
    #: The change names no key a rule selects, or the document did not change.
    NO_CHECKS = "no-checks"
    #: The target is one an alarm must never reach (prod); nothing ran and nothing was raised.
    REFUSED = "refused"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelCheckReading(_Frozen):
    """One recorded or live reading of one delegation check."""

    check_id: str = Field(min_length=1)
    verdict: EnumCheckVerdict
    read_at: datetime
    #: The workflow run the reading came from; blank when the record names none.
    run_ref: str = ""
    #: Where the reading was recorded.
    source: str = Field(min_length=1)


class ModelCheckRegressionRequest(_Frozen):
    correlation_id: str = Field(min_length=1)
    change_kind: EnumChangeKind
    #: The overlay id, the host or the repository the change is about.
    subject: str = Field(min_length=1)
    #: The keys a host or release change names; a settings-store change adds the keys its documents differ in.
    changed_keys: tuple[str, ...] = ()
    #: The documents of the settings-store version before and after; None for the other change kinds.
    previous_document: dict[str, JsonValue] | None = None
    new_document: dict[str, JsonValue] | None = None
    #: When the change applied.
    applied_at: datetime
    actor: str = Field(min_length=1)
    #: The lane the change applies to; a prod target is refused.
    target: str = Field(min_length=1)
    #: False runs the checks and delivers nothing (a probe).
    write: bool = True


class ModelCheckVerdict(_Frozen):
    check_id: str
    verdict: EnumCheckVerdict
    previous: EnumCheckVerdict | None
    run_ref: str = ""


class ModelCheckAlarm(_Frozen):
    check_id: str
    owner: str
    previous: EnumCheckVerdict | None
    current: EnumCheckVerdict
    run_ref: str
    detail: str
    #: Time from the change to the alarm.
    raised_after: timedelta
    #: True when the alarm came after the settings' alarm deadline.
    late: bool


class ModelCheckRegressionResult(_Frozen):
    correlation_id: str
    outcome: EnumCheckRegressionOutcome
    changed_keys: tuple[str, ...] = ()
    selected_checks: tuple[str, ...] = ()
    verdicts: tuple[ModelCheckVerdict, ...] = ()
    alarms: tuple[ModelCheckAlarm, ...] = ()
    #: One entry per delivered alarm: its key and its delivery steps.
    delivery: tuple[str, ...] = ()
    failure: str = ""


__all__ = [
    "EnumChangeKind",
    "EnumCheckRegressionOutcome",
    "EnumCheckVerdict",
    "ModelCheckAlarm",
    "ModelCheckReading",
    "ModelCheckRegressionRequest",
    "ModelCheckRegressionResult",
    "ModelCheckVerdict",
]
