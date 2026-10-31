# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One local alert raised or cleared: the payload of the local alert transition event.

An alert is keyed on (tenant, code, subject); the subject names the service the
alert is about, as ``<mode>/<service>``. The transition id is derived from the
transition's own fields, so a replayed transition carries the same id and the
fold recognises it. A clear carries the time the alert it clears was raised, so
the folded row is a function of the newest transition alone, whatever order the
transitions arrive in.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_local_alert_severity import EnumLocalAlertSeverity
from omnimarket.enums.enum_local_alert_transition import EnumLocalAlertTransition
from omnimarket.enums.enum_local_environment_mode import EnumLocalEnvironmentMode
from omnimarket.enums.enum_local_service_verdict import EnumLocalServiceVerdict

# A fixed namespace, so a transition id is the same wherever it is derived.
_TRANSITION_ID_NAMESPACE = uuid.UUID("5b0e4f7c-6f1e-4c9e-9a0e-19991e0a0001")


def local_alert_subject(mode: EnumLocalEnvironmentMode, service: str) -> str:
    """The subject an environment alert is about: its mode and service."""
    return f"{mode.value}/{service}"


def local_alert_transition_id(
    tenant_id: str,
    code: str,
    subject: str,
    transition: EnumLocalAlertTransition,
    occurred_at: datetime,
) -> uuid.UUID:
    """Derive a transition's id from what it says, so a replay carries the same id."""
    name = "|".join(
        (
            tenant_id,
            code,
            subject,
            transition.value,
            occurred_at.astimezone(UTC).isoformat(),
        )
    )
    return uuid.uuid5(_TRANSITION_ID_NAMESPACE, name)


class ModelLocalAlertEvidence(BaseModel):
    """What the transition was read from: the service's verdict and its report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: EnumLocalEnvironmentMode
    service: str = Field(min_length=1)
    verdict: EnumLocalServiceVerdict
    reason: str = Field(min_length=1)
    last_seen: AwareDatetime
    report_id: str | None = None


class ModelLocalAlertTransitioned(BaseModel):
    """One alert raised or cleared, with the evidence for it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1)
    code: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    transition: EnumLocalAlertTransition
    severity: EnumLocalAlertSeverity
    # When the alert this transition is about was raised; a raise's own time.
    raised_at: AwareDatetime
    occurred_at: AwareDatetime
    evidence: ModelLocalAlertEvidence
    transition_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _derive_transition_id(self) -> ModelLocalAlertTransitioned:
        if (
            self.transition is EnumLocalAlertTransition.RAISED
            and self.raised_at != self.occurred_at
        ):
            raise ValueError("a raise occurs at the time the alert is raised")
        if self.raised_at > self.occurred_at:
            raise ValueError("an alert cannot clear before it was raised")
        derived = local_alert_transition_id(
            self.tenant_id, self.code, self.subject, self.transition, self.occurred_at
        )
        if self.transition_id is None:
            object.__setattr__(self, "transition_id", derived)
        elif self.transition_id != derived:
            raise ValueError(
                "transition_id does not match the transition's tenant, code, "
                "subject, direction and time"
            )
        return self


__all__ = [
    "ModelLocalAlertEvidence",
    "ModelLocalAlertTransitioned",
    "local_alert_subject",
    "local_alert_transition_id",
]
