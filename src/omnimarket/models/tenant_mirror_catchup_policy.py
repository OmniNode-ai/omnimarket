# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How old an event may be and still be waited for by the tenant resolver (OMN-22478).

The write-time tenant resolver waits for the tenant registry mirror while the
mirror's newest observation is behind an event whose tenant it cannot see yet
(OMN-18198: a mint in flight when the event was produced, landed by the mirror
within 8.78 s at worst). The wait only helps an event still inside that race:
once an event is older than the window when the writer processes it, a mint that
was in flight has landed, and a tenant still missing is one nobody minted. On a
quiet registry the watermark stays behind every event, so without an age limit
each event from a tenant in no registry cost the whole window (about 43 minutes of
each hour on the dev lane, 128 events an hour from three tenants).

The age limit is a setting the overlay supplies, read here rather than in the
shared Settings model, which the canonical-file-shape ratchet keeps from growing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SettingsTenantMirrorCatchup(BaseSettings):
    """The overlay-supplied age limit; unset means the catch-up window itself."""

    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore", frozen=True)

    tenant_mirror_catchup_event_age_limit_seconds: float | None = Field(
        default=None,
        ge=0,
        description=(
            "An event produced longer ago than this many seconds is refused by "
            "the write-time tenant resolver without waiting for the mirror, and "
            "still dead-lettered with its cause. None means the catch-up window "
            "(MIRROR_CATCHUP_DEADLINE_SECONDS). Zero waits for no event."
        ),
    )


@lru_cache(maxsize=1)
def get_catchup_settings() -> SettingsTenantMirrorCatchup:
    return SettingsTenantMirrorCatchup()


def utc_now() -> datetime:
    return datetime.now(UTC)


class ModelEventAge(BaseModel):
    """One event's age against the catch-up window, read when it is processed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_timestamp: datetime
    age_seconds: float
    limit_seconds: float
    window_seconds: float

    @property
    def waited(self) -> bool:
        """Whether the event is young enough that the mirror may still catch up."""
        return self.age_seconds < self.limit_seconds


def assess_event_age(event_timestamp: datetime, window_seconds: float) -> ModelEventAge:
    configured = get_catchup_settings().tenant_mirror_catchup_event_age_limit_seconds
    return ModelEventAge(
        event_timestamp=event_timestamp,
        age_seconds=(utc_now() - event_timestamp).total_seconds(),
        limit_seconds=window_seconds if configured is None else configured,
        window_seconds=window_seconds,
    )


def lag_refusal_message(
    event_age: ModelEventAge,
    tenant_identity: str,
    watermark: datetime,
    mirror_table: str,
    projection_node: str,
) -> str:
    """The dead-letter cause for a mirror that is behind the event.

    The two causes need different people to act, so the message says which one
    this is and carries the watermark and the event's own timestamp.
    """
    if event_age.waited:
        return (
            f"OMN-18198: {mirror_table} holds no row for {tenant_identity!r}, AND "
            "it is still behind this event after waiting "
            f"{event_age.window_seconds:.0f}s: "
            f"the mirror's watermark is {watermark.isoformat()} while the event "
            f"was produced at {event_age.event_timestamp.isoformat()}. This is NOT the "
            "unknown-tenant refusal -- the tenant may well exist and "
            f"{projection_node} has not caught up. Measured steady-state latency "
            "of that node on the dev lane is 1.06-8.78s, so a lag past this window "
            "is a defect in the feed rather than an ordinary race. No identity "
            "will be invented or defaulted for it."
        )
    return (
        f"OMN-22478: {mirror_table} holds no row for {tenant_identity!r} and the "
        f"event is {event_age.age_seconds:.0f}s old, past the "
        f"{event_age.limit_seconds:.0f}s "
        "catch-up window, so it was not waited for: a mint in flight when it was "
        f"produced has landed by now. The mirror's watermark is "
        f"{watermark.isoformat()} while the event was produced at "
        f"{event_age.event_timestamp.isoformat()}; a quiet registry keeps the watermark "
        "behind every event, so this reads as lag but is an unknown tenant "
        f"unless {projection_node} is not consuming. No identity will be "
        "invented or defaulted for it."
    )
