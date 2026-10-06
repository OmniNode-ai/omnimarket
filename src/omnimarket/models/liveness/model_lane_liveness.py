# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane-liveness request, verdict and report models (OMN-18609).

The shapes here encode two refusals that the rest of the node depends on.

**A verdict names its own evidence.** ``evidence_basis`` is a required field on
every verdict, not an annotation. A lane reported DROPPED on the hand ledger
alone and a lane reported DROPPED on an absence of hook events are different
claims with different strengths, and a reader that cannot tell them apart will
present the weaker one with the confidence of the stronger.

**Relay silence is a first-class state, not a low count.** ``EnumRelayState``
exists because a relay outage and the simultaneous death of every lane produce
an identical signature in ``public.hook_events``. On 2026-09-17 the relay was
down for 40 minutes 52 seconds while every lane on the fleet was working
normally. A detector that reads last-event age without this state reports the
whole fleet DROPPED there.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumLaneVerdict(StrEnum):
    """What the reader concluded about one lane over one window."""

    #: Hook events attributable to the lane arrived inside the silence
    #: threshold. The lane was working.
    ALIVE = "alive"
    #: The lane has a CLAIM row, no TERMINAL row, and has gone silent past the
    #: threshold while the relay was demonstrably carrying other lanes' traffic.
    DROPPED = "dropped"
    #: The lane wrote a TERMINAL row. Ledger evidence, independent of the relay.
    TERMINATED = "terminated"
    #: The question cannot be answered from this window's data -- the events in
    #: it carry no lane attribution, so an absence of events for a named lane
    #: means nothing. Never a synonym for DROPPED.
    UNOBSERVABLE = "unobservable"
    #: The relay was silent across the window, so no lane's silence is evidence
    #: of anything. The fail-closed answer.
    UNKNOWN_RELAY_SILENT = "unknown_relay_silent"


class EnumEvidenceBasis(StrEnum):
    """Which surface a verdict actually rests on."""

    #: Hook events carrying this lane's own name.
    HOOK_EVENTS = "hook_events"
    #: CLAIM / TERMINAL rows only; hook events could not corroborate.
    LEDGER_ONLY = "ledger_only"
    #: Neither surface could answer.
    NONE = "none"


class EnumRelayState(StrEnum):
    """Whether the capture relay itself was delivering during the window."""

    #: Events of any kind arrived inside the relay threshold.
    CARRYING = "carrying"
    #: No event of any kind arrived for longer than the relay threshold.
    SILENT = "silent"


class ModelLaneObservation(BaseModel):
    """Everything known about one lane over one window, from both surfaces.

    Assembled by the caller. This node reads it and nothing else -- it performs
    no I/O, so the gatherer is responsible for the reads and this model is the
    whole interface between them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    #: When the lane's CLAIM row was appended, or ``None`` when it has none.
    claimed_at: datetime | None = None
    #: When the lane's TERMINAL row was appended, or ``None``.
    terminal_at: datetime | None = None
    #: The most recent hook event carrying this lane's name, or ``None``.
    last_hook_event_at: datetime | None = None
    #: How many hook events in the window carried this lane's name.
    hook_event_count: int = Field(default=0, ge=0)


class ModelLaneLivenessRequest(BaseModel):
    """Input to the verdict computation. Pure data; the node does no reads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_start: datetime
    window_end: datetime
    observations: tuple[ModelLaneObservation, ...] = ()

    #: The newest event of ANY kind in the window, from any lane or none.
    #: ``None`` means the relay delivered nothing at all.
    relay_last_event_at: datetime | None = None
    #: Total events in the window across every lane, used to distinguish a
    #: quiet fleet from a dead relay.
    relay_event_count: int = Field(default=0, ge=0)

    #: Whether ANY event in the window carried a lane attribution. False for
    #: every window before the emitter carried one, and the reason a historical
    #: window cannot be read by lane no matter how the thresholds are set.
    lane_attribution_available: bool = False

    #: A lane silent this long, while the relay is carrying, is dropped.
    silence_threshold_seconds: int = Field(default=900, gt=0)
    #: A relay silent this long is down, and no lane verdict may rest on
    #: silence. Deliberately shorter than the lane threshold: it must trip
    #: FIRST, or the guard it exists to provide arrives too late.
    relay_silence_threshold_seconds: int = Field(default=300, gt=0)


class ModelLaneVerdict(BaseModel):
    """One lane's verdict, with the evidence it rests on stated."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    verdict: EnumLaneVerdict
    evidence_basis: EnumEvidenceBasis
    #: Plain-language justification naming the numbers behind the verdict.
    reason: str = Field(min_length=1)
    last_hook_event_at: datetime | None = None
    hook_event_count: int = Field(default=0, ge=0)
    silent_seconds: int | None = None


class ModelLaneLivenessReport(BaseModel):
    """The projection a board renders and a scheduled job persists."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_start: datetime
    window_end: datetime
    relay_state: EnumRelayState
    relay_last_event_at: datetime | None = None
    relay_event_count: int = Field(default=0, ge=0)
    relay_silent_seconds: int | None = None
    lane_attribution_available: bool = False
    verdicts: tuple[ModelLaneVerdict, ...] = ()

    @property
    def dropped(self) -> tuple[ModelLaneVerdict, ...]:
        """Verdicts a process audit has to act on."""
        return tuple(v for v in self.verdicts if v.verdict is EnumLaneVerdict.DROPPED)

    def counts(self) -> dict[str, int]:
        """Per-verdict totals, every member present including the zeroes.

        A verdict class missing from a summary reads as "not applicable" rather
        than "none", which is how a zero-drop report and an unrun report come to
        look alike.
        """
        tally = {member.value: 0 for member in EnumLaneVerdict}
        for verdict in self.verdicts:
            tally[verdict.verdict.value] += 1
        return tally


__all__ = [
    "EnumEvidenceBasis",
    "EnumLaneVerdict",
    "EnumRelayState",
    "ModelLaneLivenessReport",
    "ModelLaneLivenessRequest",
    "ModelLaneObservation",
    "ModelLaneVerdict",
]
