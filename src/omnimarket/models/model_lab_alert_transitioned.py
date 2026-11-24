# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One lab alert raised or cleared: the payload of the lab alert transition event.

An alert is keyed on (source, subject): the source names what raises it (an alarm condition, an indicator
or a scheduled job) and the subject names what it is about (a host, a lane, a unit). The transition id is
derived from the transition's own fields, so a replayed transition carries the same id and the fold
recognises it. A clear carries the time the alert it clears was raised, so the folded row is a function of
the newest transition alone, whatever order the transitions arrive in.

Severity is the lab's own four levels (``EnumLabSliSeverity``). Owner names who must act: a lane, a team or
a person, as the source declares it. A transition with no owner, no detail or a time without a zone is refused:
the dashboard shows each of them, and an alert nobody owns is the failure this event exists to prevent.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_lab_alert_transition import EnumLabAlertTransition
from omnimarket.enums.enum_lab_sli import EnumLabSliSeverity

# A fixed namespace, so an id is the same wherever it is derived.
_ID_NAMESPACE = uuid.UUID("5b0e4f7c-6f1e-4c9e-9a0e-26663a1e0001")


def lab_alert_id(source: str, subject: str) -> str:
    """The alert's key: one per source and subject, so a repeated condition updates one row."""
    return str(uuid.uuid5(_ID_NAMESPACE, f"alert|{source}|{subject}"))


def lab_alert_transition_id(
    source: str,
    subject: str,
    transition: EnumLabAlertTransition,
    occurred_at: datetime,
) -> uuid.UUID:
    """Derive a transition's id from what it says, so a replay carries the same id."""
    name = "|".join(
        (
            "transition",
            source,
            subject,
            transition.value,
            occurred_at.astimezone(UTC).isoformat(),
        )
    )
    return uuid.uuid5(_ID_NAMESPACE, name)


class ModelLabAlertTransitioned(BaseModel):
    """One alert raised or cleared, with who owns it and how bad it is."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    source: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    transition: EnumLabAlertTransition
    severity: EnumLabSliSeverity
    owner: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    # When the alert this transition is about was raised; a raise's own time.
    raised_at: AwareDatetime
    occurred_at: AwareDatetime
    transition_id: uuid.UUID | None = None

    @property
    def alert_id(self) -> str:
        return lab_alert_id(self.source, self.subject)

    @model_validator(mode="after")
    def _derive_transition_id(self) -> ModelLabAlertTransitioned:
        if (
            self.transition is EnumLabAlertTransition.RAISED
            and self.raised_at != self.occurred_at
        ):
            raise ValueError("a raise occurs at the time the alert is raised")
        if self.raised_at > self.occurred_at:
            raise ValueError("an alert cannot clear before it was raised")
        derived = lab_alert_transition_id(
            self.source, self.subject, self.transition, self.occurred_at
        )
        if self.transition_id is None:
            object.__setattr__(self, "transition_id", derived)
        elif self.transition_id != derived:
            raise ValueError(
                "transition_id does not match the transition's source, subject, "
                "direction and time"
            )
        return self


__all__ = [
    "ModelLabAlertTransitioned",
    "lab_alert_id",
    "lab_alert_transition_id",
]
