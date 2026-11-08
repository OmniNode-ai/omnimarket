# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide which cause lanes a THROUGHPUT STALL alarm starts, without I/O (OMN-22885).

The throughput tick wrote 59 stall MSG rows to the foreground orchestrator from 00:08Z to 10:30Z
on 2026-11-07, 5 to 6 an hour, and the only reader was a foreground turn. Each row names the
findings that stalled the tick (`floor:omnibase_core, lab-headroom:h201, ...`). This handler
reads those rows, takes each finding as a cause key (`<class>:<subject>`) and answers one
dispatch per distinct key.

The MSG cell is cut at 220 characters, so the last finding of a cut row is a fragment of a key;
it is dropped and counted, never dispatched. A key the tick relaunches itself (a no-host
dispatch) starts nothing, and a key a live lane of its own class already covers starts nothing.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta

from omnimarket.handlers.ledger_row_grammar import parse_row
from omnimarket.nodes.node_stall_responder_compute.models import (
    ModelCauseDispatch,
    ModelCauseSkip,
    ModelRowRefusal,
    ModelStallResponderRequest,
    ModelStallResponderResult,
)

TICK_SENDER = "launchd-throughput-tick"
STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
STALL = re.compile(
    r"THROUGHPUT STALL n=\d+ unknown=\d+: (?P<found>.*?)(?P<cut>\.\.\.)?\. Full output:"
)
CAUSE_CLASS = re.compile(r"^[a-z][a-z0-9-]*$")
# Findings whose relaunch the tick performs itself (a no-host dispatch is re-placed, queued or
# expired there).
TICK_OWNED_CLASSES = frozenset(
    {
        "dispatch",
        "dispatch-replace",
        "dispatch-queue",
        "dispatch-queue-expired",
        "dispatch-lost",
    }
)
# A lane the tick already starts for a class of its own, named by its prefix.
CLASS_LANE_PREFIX = {"floor": "floor-diagnose-"}
LANE_PREFIX = "stall"
SLUG_LIMIT = 40
DIGEST_LENGTH = 8
# The stamp a cause lane's name ends with (`lane_name`), in slug form (lower case).
LANE_STAMP = re.compile(r"\d{8}t\d{4}z")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _subject_part(subject: str) -> str:
    """The subject's share of a lane stem, at most SLUG_LIMIT characters.

    A subject that fits is its slug. A longer one is cut and given a digest of the whole
    subject, so two subjects sharing their first SLUG_LIMIT characters keep distinct stems."""
    slug = _slug(subject)
    if len(slug) <= SLUG_LIMIT:
        return slug
    digest = hashlib.sha256(subject.encode()).hexdigest()[:DIGEST_LENGTH]
    return f"{slug[: SLUG_LIMIT - DIGEST_LENGTH - 1].strip('-')}-{digest}"


def _moment(stamp: str) -> datetime:
    return datetime.strptime(stamp, STAMP_FORMAT).replace(tzinfo=UTC)


def split_cause(key: str) -> tuple[str, str]:
    """(class, subject) of a cause key: `floor:omnibase_core` is ('floor', 'omnibase_core')."""
    cause_class, _, subject = key.partition(":")
    return cause_class, subject


def lane_stem(key: str) -> str:
    cause_class, subject = split_cause(key)
    return "-".join(
        part for part in (LANE_PREFIX, cause_class, _subject_part(subject)) if part
    )


def lane_name(key: str, stamp: str) -> str:
    return f"{lane_stem(key)}-{_moment(stamp).strftime('%Y%m%dT%H%MZ')}"


def _covering_lane(key: str, lanes: tuple[str, ...]) -> str | None:
    """The live lane named `<stem>-<stamp>` for this key, matched whole: a lane of a longer
    or a similarly prefixed subject does not cover it."""
    cause_class, subject = split_cause(key)
    prefix = CLASS_LANE_PREFIX.get(cause_class)
    stems = [lane_stem(key)]
    if prefix and subject:
        stems.append(_slug(f"{prefix}{subject}"))
    for lane in lanes:
        slug = _slug(lane)
        for stem in stems:
            head, _, stamp = slug.rpartition("-")
            if head == stem and LANE_STAMP.fullmatch(stamp):
                return lane
    return None


class HandlerStallCauseResponder:
    """Pure: stall MSG rows in, one dispatch per distinct uncovered cause key out."""

    def handle(self, request: ModelStallResponderRequest) -> ModelStallResponderResult:
        stalls: list[tuple[str, str, str]] = []  # (stamp, ticket, key), one per finding
        stall_rows = unreadable = truncated = 0
        refusals: list[ModelRowRefusal] = []
        for line in dict.fromkeys(
            line.strip() for line in request.rows if line.strip()
        ):
            row = parse_row(line, types=frozenset({"MSG"}))
            if row is None or row.value("from") != TICK_SENDER:
                continue
            text = row.cells[-1] if row.cells else ""
            if "THROUGHPUT STALL" not in text:
                continue
            match = STALL.search(text)
            if match is None:
                unreadable += 1
                continue
            try:
                _moment(row.stamp)
            except ValueError:
                refusals.append(
                    ModelRowRefusal(stamp=row.stamp, reason="invalid-calendar-date")
                )
                continue
            stall_rows += 1
            keys = [key.strip() for key in match.group("found").split(", ")]
            if match.group("cut"):
                truncated += 1
                keys = keys[:-1]
            ticket = row.value("ticket") or ""
            stalls.extend((row.stamp, ticket, key) for key in keys if key)
        stalls.sort(key=lambda item: item[0])

        by_key: dict[str, list[tuple[str, str]]] = {}
        for stamp, ticket, key in stalls:
            by_key.setdefault(key, []).append((stamp, ticket))

        window = timedelta(hours=request.redispatch_after_hours)
        dispatched = dict(request.dispatched)
        dispatches: list[ModelCauseDispatch] = []
        skipped: list[ModelCauseSkip] = []
        for key, seen in by_key.items():
            cause_class, subject = split_cause(key)
            if not CAUSE_CLASS.match(cause_class):
                skipped.append(
                    ModelCauseSkip(
                        cause_key=key, reason="no-cause-class", occurrences=len(seen)
                    )
                )
                continue
            if cause_class in TICK_OWNED_CLASSES:
                skipped.append(
                    ModelCauseSkip(
                        cause_key=key,
                        reason="tick-owns-relaunch",
                        occurrences=len(seen),
                    )
                )
                continue
            prior = dispatched.get(key)
            if prior is None:
                trigger = seen[0]
            else:
                try:
                    due = _moment(prior) + window
                except ValueError:
                    skipped.append(
                        ModelCauseSkip(
                            cause_key=key,
                            reason="invalid-dispatched-stamp",
                            occurrences=len(seen),
                        )
                    )
                    continue
                later = [item for item in seen if _moment(item[0]) >= due]
                if not later:
                    skipped.append(
                        ModelCauseSkip(
                            cause_key=key,
                            reason="already-dispatched",
                            occurrences=len(seen),
                        )
                    )
                    continue
                # The newest row past the window: a replay of old rows catches up in one
                # call instead of dispatching for one historical window per call.
                trigger = later[-1]
            covering = _covering_lane(key, request.covered_lanes)
            if covering is not None:
                skipped.append(
                    ModelCauseSkip(
                        cause_key=key,
                        reason=f"covered:{covering}",
                        occurrences=len(seen),
                    )
                )
                continue
            stamp, ticket = trigger
            dispatched[key] = stamp
            dispatches.append(
                ModelCauseDispatch(
                    cause_key=key,
                    cause_class=cause_class,
                    subject=subject,
                    lane=lane_name(key, stamp),
                    ticket=ticket,
                    first_stamp=stamp,
                    occurrences=len(seen),
                )
            )
        return ModelStallResponderResult(
            dispatches=tuple(dispatches),
            skipped=tuple(skipped),
            dispatched=dispatched,
            stall_rows=stall_rows,
            unreadable_rows=unreadable,
            truncated_findings=truncated,
            refusals=tuple(refusals),
        )
