"""Typed input and output of the stuck plugin version bump judgement (OMN-20497).

Frozen dataclasses, not pydantic: queue-status calls the handler from the offline suites, where only the standard
library is installed.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from omnimarket.models.plugin_version_bump import BUMP_BRANCH as BUMP_BRANCH

# A bump runs validate-plugin on its pull request and again in the merge queue, about 25 minutes each; past two of
# those and a tick it is stuck. On 2026-10-18 a bump pull request sat open 90 minutes, rebuilt on every merge,
# and six merged fixes ran on no host with no alarm.
BUMP_STUCK_AFTER = timedelta(minutes=90)


@dataclass(frozen=True)
class ModelPluginBumpStuckRequest:
    watcher: (
        Mapping[str, Any] | None
    )  # the PR watcher state's raw dict; None when it is unread or stale
    now: datetime
    branch: str = BUMP_BRANCH
    stuck_after: timedelta = BUMP_STUCK_AFTER


@dataclass(frozen=True)
class ModelPluginBumpStuckResult:
    lines: list[str] = field(default_factory=list)  # the queue-status ``bump`` lines
    reasons: list[str] = field(
        default_factory=list
    )  # DEGRADED reasons, one per stuck bump
    actions: list[str] = field(default_factory=list)  # one per reason
