# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the lab alarm's delivery (OMN-23906).

One request carries what one condition read in one run: its outcome and every subject that is alarming.
Delivery is edge-triggered: a subject is delivered when it ENTERS the alarm state, not again until its
condition reads OK (the alarm script's rule, read from scripts/lab_alarm.py ``select_new_alarms``).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumDeliveryCondition(StrEnum):
    """What the condition read. Only OK clears an episode; INDETERMINATE is not a recovery."""

    OK = "OK"
    ALARM = "ALARM"
    INDETERMINATE = "INDETERMINATE"


class EnumDeliveryStep(StrEnum):
    #: The channel or the ledger took it.
    DELIVERED = "delivered"
    #: The step was tried and failed; the ledger step is retried on the next request, the channel step is not.
    FAILED = "failed"
    #: Dedupe: the subject is already in an episode, or its ledger row was written inside the window.
    SUPPRESSED = "suppressed"
    #: The ledger already held the row (the ledger host deduplicates on the request id).
    DUPLICATE = "duplicate"
    #: The step was not run for this alarm (an episode that was already delivered).
    NOT_DUE = "not-due"


class EnumDeliveryOutcome(StrEnum):
    #: At least one step ran and none failed.
    DELIVERED = "delivered"
    #: Nothing new: every subject is in an episode already delivered, or the condition read OK/INDETERMINATE.
    NOTHING_NEW = "nothing-new"
    #: A channel or ledger step failed; the failure names which.
    FAILED = "failed"
    #: The request did not write (a probe): nothing was delivered and no state moved.
    SKIPPED = "skipped"


class ModelLabAlarm(BaseModel):
    """One subject that is in an alarm state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The condition's name, as the run record carries it (``consumer_group_lag`` for the broker lag).
    condition: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    #: What a reader can act on; a blank detail is a refused alarm.
    detail: str = Field(min_length=1)

    @property
    def key(self) -> str:
        """One alarm per subject per condition: the identity the edge-triggering keys on."""
        return f"{self.condition}:{self.subject}"


class ModelLabAlarmDeliveryRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    condition: str = Field(min_length=1)
    outcome: EnumDeliveryCondition
    #: Every subject alarming now; each must be of ``condition``.
    alarms: tuple[ModelLabAlarm, ...] = ()
    #: The condition's own reading, quoted in the ledger row.
    evidence: str = ""
    #: False delivers nothing and keeps nothing (a probe or a drill).
    write: bool = True

    @model_validator(mode="after")
    def _shape(self) -> ModelLabAlarmDeliveryRequest:
        if any(a.condition != self.condition for a in self.alarms):
            raise ValueError(
                "an alarm belongs to a different condition than the request"
            )
        if self.alarms and self.outcome is not EnumDeliveryCondition.ALARM:
            raise ValueError("alarms are carried only by an ALARM outcome")
        return self


class ModelAlarmDelivery(BaseModel):
    """What happened to one subject."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    channel: EnumDeliveryStep
    #: The channel message reference (a Slack message timestamp) when the channel took it.
    channel_ref: str = ""
    ledger: EnumDeliveryStep
    #: The ledger request id the row was appended under.
    ledger_request_id: str = ""
    failure: str = ""


class ModelLabAlarmDeliveryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    condition: str
    outcome: EnumDeliveryOutcome
    deliveries: tuple[ModelAlarmDelivery, ...]
    #: Episode keys the condition's OK ended.
    cleared: tuple[str, ...]
    #: Ledger rows an earlier request could not write that this one retried.
    retried: tuple[str, ...]
    state_kept: bool
    failure: str = ""


__all__ = [
    "EnumDeliveryCondition",
    "EnumDeliveryOutcome",
    "EnumDeliveryStep",
    "ModelAlarmDelivery",
    "ModelLabAlarm",
    "ModelLabAlarmDeliveryRequest",
    "ModelLabAlarmDeliveryResult",
]
