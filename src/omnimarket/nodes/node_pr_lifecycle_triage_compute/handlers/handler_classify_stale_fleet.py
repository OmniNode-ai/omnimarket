# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stale-fleet red class: a red whose failing check's job log names runner label sets carried ONLY by
offline runners, once fleet-status reads those labels carried by an online runner.

Pure helpers, no I/O: ``offline_only_label_sets`` reads the label sets a job log says only offline runners carry,
and ``is_stale_fleet`` decides whether every red check's log names such sets and an online runner now carries
each of them. ``handler_classify_landing_red`` calls ``is_stale_fleet`` in its first-match order; the controller
reads the job log and fleet-status and passes them as facts.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from itertools import pairwise

# A job log line is ``<ISO timestamp> <text>``; the runner-label gate prints a ``labels:`` line and, on the next
# line, a ``status:`` line for each runs-on label set it judged.
_STAMP = r"^(?:\S+Z\s+)?"
_LABELS_RE = re.compile(_STAMP + r"labels:\s+(?P<labels>.+?)\s*$")
_STATUS_RE = re.compile(_STAMP + r"status:\s+(?P<status>.+)$")
OFFLINE_ONLY_PHRASE = "carried ONLY by offline runner"


def offline_only_label_sets(log: str) -> tuple[frozenset[str], ...]:
    """The distinct label sets a job log says are carried ONLY by offline runners, sorted by their labels."""
    lines = [ln for ln in log.splitlines() if ln.strip()]
    found: set[frozenset[str]] = set()
    for line, following in pairwise(lines):
        labels = _LABELS_RE.match(line)
        status = _STATUS_RE.match(following)
        if labels is None or status is None:
            continue
        if OFFLINE_ONLY_PHRASE not in status.group("status"):
            continue
        names = frozenset(
            part.strip() for part in labels.group("labels").split(",") if part.strip()
        )
        if names:
            found.add(names)
    return tuple(sorted(found, key=sorted))


def is_stale_fleet(
    checks: Iterable[str],
    failure_logs: Mapping[str, str | None] | None,
    online_runner_labels: Sequence[Sequence[str]] | None,
) -> bool:
    """Every red check's log names offline-only label sets and an online runner carries each set whole.

    An unread fleet (``online_runner_labels`` None), a check with no log, a log that names no offline-only
    label set, and a label set no single online runner carries whole are all False: those reds keep their
    existing class.
    """
    names = [str(c) for c in checks]
    if not names or failure_logs is None or online_runner_labels is None:
        return False
    online = [frozenset(labels) for labels in online_runner_labels]
    for name in names:
        log = failure_logs.get(name)
        if log is None:
            return False
        wanted = offline_only_label_sets(log)
        if not wanted:
            return False
        if not all(any(w <= runner for runner in online) for w in wanted):
            return False
    return True


__all__: list[str] = [
    "OFFLINE_ONLY_PHRASE",
    "is_stale_fleet",
    "offline_only_label_sets",
]
