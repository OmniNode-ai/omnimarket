# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure fold that decides the savings_estimates identity a delegation run is written under.

One delegation run has one saving (OMN-20303). The run emits two terminals --
the delegate-skill terminal and the canonical delegation terminal -- and each
carries its own time, so the identity key
``(session_id, event_timestamp, model_local, model_cloud_baseline)`` differed
between them and one run became two rows. The fold answers with the run's
stored identity when the run already has a row, and with the incoming
terminal's own identity otherwise. Among several stored rows (written before
this fold existed) it answers with the earliest, so every later terminal
converges on one row instead of adding another.

The effect writers read the run's stored identities, call this, and upsert
under what it returns; they decide nothing.
"""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelSavingsRunIdentity(BaseModel):
    """The non-session half of a savings_estimates identity key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_timestamp: AwareDatetime
    model_local: str = Field(min_length=1)
    model_cloud_baseline: str = Field(min_length=1)


class ModelSavingsRunIdentityFoldRequest(BaseModel):
    """The incoming terminal's identity and the identities stored for its run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    incoming: ModelSavingsRunIdentity
    stored: tuple[ModelSavingsRunIdentity, ...] = Field(default=())


class HandlerSavingsRunIdentityFold:
    """Fold a delegation run's savings terminals onto one identity."""

    def handle(
        self, request: ModelSavingsRunIdentityFoldRequest
    ) -> ModelSavingsRunIdentity:
        """Return the stored identity of the run's earliest row, else the incoming one."""
        if not request.stored:
            return request.incoming
        return min(
            request.stored,
            key=lambda identity: (
                identity.event_timestamp,
                identity.model_local,
                identity.model_cloud_baseline,
            ),
        )


__all__ = [
    "HandlerSavingsRunIdentityFold",
    "ModelSavingsRunIdentity",
    "ModelSavingsRunIdentityFoldRequest",
]
