# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The post gate: decides, per message, whether the alert channel posts it (OMN-24562).

Pure over (policy, state, now): the state is a plain dict the caller keeps between runs. One post per
condition (the dedup key names no host for a lab-wide kind), re-posted when the message's fingerprint changes
or once per reminder interval, one resolved post when it clears, nothing for a self-healing or unreadable
message until the detector has been blind longer than the overlay's hours, and a ticket (not a post) for an
episode open past the ticket age.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any

from omnimarket.models.alarm_post_policy import (
    EnumAlarmClass,
    EnumPostAction,
    ModelAlarmKindPolicy,
    ModelAlarmPostPolicy,
    ModelPostDecision,
)

_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_OPEN = "open"
_BLIND = "blind"
_DIGITS = re.compile(r"\d+")
_SPACE = re.compile(r"\s+")


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(_STAMP)


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, _STAMP).replace(tzinfo=UTC)


def fingerprint(text: str) -> str:
    """What the condition says, with its counters removed: a changed run count is not a changed condition."""
    return hashlib.sha256(
        _SPACE.sub(" ", _DIGITS.sub("#", text)).strip().lower().encode()
    ).hexdigest()[:16]


class AlarmPostGate:
    """Decide each message against the policy; ``state`` is mutated in place and is what the caller persists."""

    def __init__(self, policy: ModelAlarmPostPolicy) -> None:
        self._policy = policy
        self._compiled = tuple(
            (kind, re.compile(kind.match, re.S)) for kind in policy.kinds
        )

    def _classify(self, text: str) -> tuple[ModelAlarmKindPolicy | None, str, str]:
        for kind, pattern in self._compiled:
            found = pattern.search(text)
            if found is None:
                continue
            subject = (found.groupdict().get("subject") or "").strip()
            return kind, subject, kind.dedup_key.format(kind=kind.kind, subject=subject)
        digest = fingerprint(text)
        return None, "", f"unmatched:{digest}"

    def decide(
        self, text: str, *, now: datetime, state: dict[str, Any]
    ) -> ModelPostDecision:
        kind, subject, key = self._classify(text)
        name = kind.kind if kind else "unmatched"
        episodes: dict[str, dict[str, Any]] = state.setdefault(_OPEN, {})
        blind: dict[str, str] = state.setdefault(_BLIND, {})

        if kind is not None:
            if any(re.search(p, text, re.S) for p in kind.self_healing):
                return ModelPostDecision(
                    action=EnumPostAction.SUPPRESS,
                    kind=name,
                    key=key,
                    reason="self-healing",
                )
            if any(re.search(p, text, re.S) for p in kind.unreadable):
                first = blind.setdefault(name, _iso(now))
                limit = timedelta(hours=self._policy.unreadable_post_after_hours)
                if now - _parse(first) >= limit and blind.get(f"{name}:posted") is None:
                    blind[f"{name}:posted"] = _iso(now)
                    return ModelPostDecision(
                        action=EnumPostAction.DETECTOR_DOWN,
                        kind=name,
                        key=f"{name}:detector",
                        reason="unreadable",
                        text=self._policy.detector_down_template.format(
                            kind=name,
                            subject=subject,
                            action=kind.action,
                            hours=self._policy.unreadable_post_after_hours,
                        ),
                    )
                return ModelPostDecision(
                    action=EnumPostAction.SUPPRESS,
                    kind=name,
                    key=key,
                    reason="unreadable",
                )
            # A readable message ends the detector's blindness.
            blind.pop(name, None)
            blind.pop(f"{name}:posted", None)
            alarm_class = kind.alarm_class
            reminder = timedelta(hours=kind.reminder_hours)
        else:
            alarm_class = self._policy.unmatched_class
            reminder = timedelta(hours=self._policy.unmatched_reminder_hours)

        if alarm_class is EnumAlarmClass.QUIET:
            return ModelPostDecision(
                action=EnumPostAction.SUPPRESS, kind=name, key=key, reason="quiet"
            )

        print_fp = fingerprint(text)
        known_open_for = timedelta(0)
        if kind is not None and kind.open_minutes:
            said = re.search(kind.open_minutes, text, re.S)
            if said is not None:
                known_open_for = timedelta(minutes=int(said.group("minutes")))
        episode = episodes.get(key)
        ticket_age = timedelta(hours=self._policy.ticket_after_hours)
        if episode is None:
            episode = {"first": _iso(now - known_open_for), "last_post": None, "fp": ""}
            episodes[key] = episode
        elif known_open_for:
            episode["first"] = min(episode["first"], _iso(now - known_open_for))
        if episode.get("ticketed"):
            return ModelPostDecision(
                action=EnumPostAction.SUPPRESS, kind=name, key=key, reason="ticketed"
            )
        if now - _parse(episode["first"]) >= ticket_age:
            episode["ticketed"] = _iso(now)
            return ModelPostDecision(
                action=EnumPostAction.TICKET,
                kind=name,
                key=key,
                reason="stale-no-owner",
                text=text,
            )
        if episode["last_post"] is None:
            episode.update(last_post=_iso(now), fp=print_fp, text=text)
            return ModelPostDecision(
                action=EnumPostAction.POST, kind=name, key=key, reason="new", text=text
            )
        if episode["fp"] != print_fp:
            episode.update(last_post=_iso(now), fp=print_fp, text=text)
            return ModelPostDecision(
                action=EnumPostAction.POST,
                kind=name,
                key=key,
                reason="changed",
                text=text,
            )
        if now - _parse(episode["last_post"]) >= reminder:
            episode["last_post"] = _iso(now)
            return ModelPostDecision(
                action=EnumPostAction.REMINDER,
                kind=name,
                key=key,
                reason="reminder",
                text=text,
            )
        return ModelPostDecision(
            action=EnumPostAction.SUPPRESS, kind=name, key=key, reason="duplicate"
        )

    def resolve(
        self, kind_name: str, subject: str, *, now: datetime, state: dict[str, Any]
    ) -> ModelPostDecision:
        """The condition read clear: one resolved post when its kind asks for one and it had posted."""
        kind = next((k for k in self._policy.kinds if k.kind == kind_name), None)
        key = (
            kind.dedup_key.format(kind=kind_name, subject=subject)
            if kind
            else f"{kind_name}:{subject}"
        )
        episode = state.setdefault(_OPEN, {}).pop(key, None)
        if episode is None or episode.get("last_post") is None:
            return ModelPostDecision(
                action=EnumPostAction.SUPPRESS,
                kind=kind_name,
                key=key,
                reason="cleared",
            )
        if kind is None or not kind.post_resolved:
            return ModelPostDecision(
                action=EnumPostAction.SUPPRESS,
                kind=kind_name,
                key=key,
                reason="cleared",
            )
        hours = int((now - _parse(episode["first"])).total_seconds() // 3600)
        return ModelPostDecision(
            action=EnumPostAction.RESOLVED,
            kind=kind_name,
            key=key,
            reason="cleared",
            text=self._policy.resolved_template.format(
                kind=kind_name, subject=subject, action=kind.action, hours=hours
            ),
        )


def run_ticket_command(command: tuple[str, ...], decision: ModelPostDecision) -> bool:
    """Run the overlay's ticket command for a stale episode; True when it exited 0."""
    argv = [
        part.format(kind=decision.kind, subject=decision.key, text=decision.text[:500])
        for part in command
    ]
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


__all__ = ["AlarmPostGate", "fingerprint", "run_ticket_command"]
