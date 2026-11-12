# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the rotation tick is asked, reads, decides and records (OMN-23000).

No model here holds a token: a host's login is identified by the label and fingerprint the pool already
shows, and every event names labels only. The overlay holds every setting; the spread rule is
declared only when a host wants it, and ``None`` means it is off.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.models.model_claude_pool_limit_recheck import (
    ModelLimitRecheckPolicy,
)
from omnimarket.models.model_claude_pool_revoke_staleness import (
    ModelRevokeStalenessPolicy,
)


class ModelRotationMark(BaseModel):
    """One label's marks at the source: a limit that ends at ``until`` and whether its login was revoked."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    #: A UTC instant as ``%Y-%m-%dT%H:%M:%SZ``: the pool orders limit times as text, so any accepted
    #: instant (with an offset, or fractional seconds) is normalised to that form, and a value that
    #: is not an instant, or names no zone, is refused rather than kept as a limit that never ends.
    until: str | None = None
    revoked: bool = False
    #: Optional facts a newer source mirror carries so a host can tell a stale revoke from a real one; an
    #: older mirror without them reads exactly as before (the stale-revoke rule then has nothing to go on).
    fingerprint: str | None = None
    revoked_at: str | None = None
    limited_at: str | None = None

    @field_validator("until", "revoked_at", "limited_at")
    @classmethod
    def _until_is_a_utc_instant(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            instant = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("until is not an ISO 8601 instant") from None
        if instant.tzinfo is None:
            raise ValueError("until names no time zone")
        return instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class ModelRotationMarksRead(BaseModel):
    """What reading the one source gave: marks and the time it wrote them, or why it could not be read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["read", "unreadable"]
    at: datetime | None = None
    marks: tuple[ModelRotationMark, ...] = ()
    detail: str = ""


class ModelRotationHost(BaseModel):
    """A host the tick is declared for. Its place in the overlay's host list is its rank."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    scheduler: Literal["systemd-user", "launchd"]
    #: The venv whose interpreter runs the installed unit, relative to the registry root or absolute.
    venv: str = Field(min_length=1)
    #: The login the host's lanes read, relative to the user's home.
    token_file: str = Field(min_length=1)


class ModelRotationMarksSource(BaseModel):
    """The one source of limited and revoked marks: a host's pool status mirror, read in place or over ssh."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    #: What ssh dials to reach the source host; unset reads the file on this host.
    ssh_target: str | None
    #: The status mirror the source host's pool writes, relative to the user's home.
    mirror_path: str = Field(min_length=1)
    #: A source older than this is refused, so a dead source host never erases a mark.
    max_age_seconds: int = Field(ge=1)


class ModelRotationSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    interval_seconds: int = Field(ge=60)


class ModelRotationSpread(BaseModel):
    """When a usable login has been in use longer than the bound, the tick spreads it to an unused token.

    One account's limit then stops benching one host's lanes alone: the host that has used it the
    longest moves to a token no host's lanes have used yet.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool
    in_use_longer_than_seconds: int = Field(ge=60)


class ModelRotationSteering(BaseModel):
    """Account steering for NEW lane starts: the switch, the usage verdict to ask and the lanes-per-label cap.

    ``enabled`` is the switch. Off, a lane start draws exactly as it did before the block existed: nothing
    is asked and nothing is passed to ``resolve``. On, the lane start runs ``verdict_argv`` (the command
    that prints the usage verdict node's result as JSON), passes its ``drain_labels`` to ``resolve``, and
    counts the lanes running under each label from the open CLAIM rows no older than
    ``open_claim_max_age_seconds``. ``max_lanes_per_label`` is the hard cap ``resolve`` holds those counts
    against; unset, a label carries any number of lanes. A cap needs the counts, so it needs the switch on.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool
    max_lanes_per_label: int | None = Field(default=None, ge=1)
    verdict_argv: tuple[str, ...] = ()
    verdict_timeout_seconds: float | None = Field(default=None, gt=0)
    open_claim_max_age_seconds: int | None = Field(default=None, ge=60)

    @model_validator(mode="after")
    def _on_needs_its_values(self) -> ModelRotationSteering:
        if self.enabled:
            missing = [
                name
                for name, value in (
                    ("verdict_argv", self.verdict_argv),
                    ("verdict_timeout_seconds", self.verdict_timeout_seconds),
                    ("open_claim_max_age_seconds", self.open_claim_max_age_seconds),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    f"steering.enabled is true and {', '.join(missing)} is not set"
                )
        elif self.max_lanes_per_label is not None:
            raise ValueError(
                "steering.max_lanes_per_label is set and steering.enabled is false: "
                "the cap is held against lane counts only a switched-on lane start passes"
            )
        return self


class ModelLimitProbeSettings(BaseModel):
    """The pool's own headless probe of one limited label: the call, its deadline and how its answer is read.

    ``limited_patterns`` mark a usage-limit answer, ``reset_pattern`` reads the reset the answer names (named
    groups ``month``, ``day``, ``hour``, optional ``minute`` and ``ampm``, in UTC), and ``reset_margin_seconds`` is
    added to it the way a limit stamp always carries a margin. An answer that is neither a limit nor a clean
    reply is an ``error`` and proves nothing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    limited_patterns: tuple[str, ...] = Field(min_length=1)
    reset_pattern: str = Field(min_length=1)
    reset_margin_seconds: int = Field(ge=0)

    @field_validator("reset_pattern")
    @classmethod
    def _reset_pattern_names_its_groups(cls, value: str) -> str:
        import re

        try:
            groups = set(re.compile(value).groupindex)
        except re.error as bad:
            raise ValueError(
                f"reset_pattern is not a regular expression: {bad}"
            ) from None
        missing = {"month", "day", "hour"} - groups
        if missing:
            raise ValueError(
                f"reset_pattern lacks the named groups {', '.join(sorted(missing))}"
            )
        return value


class ModelLimitRecheckSettings(BaseModel):
    """The limit-recheck rule: how often a limited label is re-tested and the probe that re-tests it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy: ModelLimitRecheckPolicy
    probe: ModelLimitProbeSettings


class ModelRotationOverlay(BaseModel):
    """``<root>/node_claude_token_pool_effect/overlay.yaml``: the hosts, the source, the events file and the interval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hosts: tuple[ModelRotationHost, ...] = Field(min_length=1)
    marks_source: ModelRotationMarksSource
    #: Where rotations are appended, relative to the unit's state directory.
    events_file: str = Field(min_length=1)
    unit_environment: dict[str, str]
    schedule: ModelRotationSchedule
    #: When set and enabled, a usable login in use longer than the bound is spread to an unused token.
    #: ``None`` declares no spread rule: the tick only swaps a login that is limited or revoked.
    spread: ModelRotationSpread | None = None
    #: Account steering for new lane starts. ``None`` (or ``enabled: false``) declares none: a lane start
    #: draws as before, and no label has a lane cap.
    steering: ModelRotationSteering | None = None
    #: The revoke-staleness rule. Absent (``None``) => no revoke is ever cleared as stale (not configured);
    #: present => its ``margin_seconds`` decides which revokes are stale enough to clear.
    stale_revoke: ModelRevokeStalenessPolicy | None = None
    #: The limit-recheck rule (OMN-23895). Absent => a limited mark is never re-tested (not configured);
    #: present => on the marks-source host the tick probes a limited label every ``policy.interval_seconds``.
    limit_recheck: ModelLimitRecheckSettings | None = None

    @model_validator(mode="after")
    def _shape(self) -> ModelRotationOverlay:
        names = [host.name for host in self.hosts]
        if len(set(names)) != len(names):
            raise ValueError("overlay hosts name a host twice")
        if self.marks_source.host not in names:
            raise ValueError("marks_source.host is not one of the overlay hosts")
        return self

    def rank(self, name: str) -> int | None:
        return next((i for i, h in enumerate(self.hosts) if h.name == name), None)

    def host(self, name: str) -> ModelRotationHost | None:
        return next((h for h in self.hosts if h.name == name), None)


class ModelClaudeTokenRotationRequest(BaseModel):
    """One tick on ``host``. ``dry_run`` decides and writes nothing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    dry_run: bool = False


class ModelRotationMarksSync(BaseModel):
    """What re-syncing the pool's marks from the source did."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["applied", "unchanged", "unreadable", "stale", "is_source"]
    changed: tuple[str, ...] = ()
    detail: str = ""


class ModelRotationEvent(BaseModel):
    """One line of the rotation record: a host moved off a dead login, or found none to move to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["rotated", "rotation_failed", "no_usable", "spread"]
    host: str
    from_label: str | None
    to_label: str | None
    reason: Literal["limited", "revoked", "absent", "spread", "assigned"]
    at: datetime


class ModelClaudeTokenRotationResult(BaseModel):
    """The tick's decision. Labels and states only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    rank: int
    outcome: Literal["kept", "swapped", "unmanaged", "no_usable", "planned", "spread"]
    in_use_state: Literal["usable", "limited", "revoked", "unmanaged", "absent"]
    from_label: str | None = None
    to_label: str | None = None
    order: tuple[str, ...] = ()
    marks_sync: ModelRotationMarksSync
    events: tuple[ModelRotationEvent, ...] = ()
    alarm: str | None = None
    lines: tuple[str, ...] = ()
    #: ``not_configured`` when the overlay has no stale-revoke block (the old behaviour, never an error);
    #: ``evaluated`` when the rule ran and decided which revokes to clear.
    stale_revoke: Literal["not_configured", "evaluated"] = "not_configured"
    #: Labels whose stale revokes the tick cleared (labels only, never a token).
    revokes_cleared: tuple[str, ...] = ()
    #: Labels whose revokes the tick kept (real or unknown, so left in place).
    revokes_kept: tuple[str, ...] = ()
    #: ``not_configured`` when the overlay has no limit-recheck block, or this host does not hold the marks;
    #: ``evaluated`` when the rule ran.
    limit_recheck: Literal["not_configured", "evaluated"] = "not_configured"
    #: Labels whose limited mark the tick cleared on an ok probe, replaced by a newer reset, and kept (limited or
    #: error answer). Labels only.
    limits_cleared: tuple[str, ...] = ()
    limits_replaced: tuple[str, ...] = ()
    limits_kept: tuple[str, ...] = ()


class ModelClaudeTokenAssignRequest(BaseModel):
    """Install the pool token named by ``label`` on ``host``'s login. ``dry_run`` decides and writes nothing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    label: str = Field(min_length=1)
    dry_run: bool = False


class ModelClaudeTokenAssignResult(BaseModel):
    """The assign's decision. Labels only; a refusal names why, in the pool's own vocabulary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    label: str = Field(min_length=1)
    outcome: Literal["assigned", "unchanged", "refused"]
    refusal: Literal["limited", "revoked", "unknown_label", "none"] = "none"
    from_label: str | None = None
    #: The label is held first in the host's draw until released, so the pool's resolve hands it to new lanes.
    pinned: bool = False
    lines: tuple[str, ...] = ()


__all__ = [
    "ModelClaudeTokenAssignRequest",
    "ModelClaudeTokenAssignResult",
    "ModelClaudeTokenRotationRequest",
    "ModelClaudeTokenRotationResult",
    "ModelLimitProbeSettings",
    "ModelLimitRecheckSettings",
    "ModelRotationEvent",
    "ModelRotationHost",
    "ModelRotationMark",
    "ModelRotationMarksRead",
    "ModelRotationMarksSource",
    "ModelRotationMarksSync",
    "ModelRotationOverlay",
    "ModelRotationSchedule",
    "ModelRotationSpread",
    "ModelRotationSteering",
]
