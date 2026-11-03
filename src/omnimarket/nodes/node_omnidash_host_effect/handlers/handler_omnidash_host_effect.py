# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One updater tick: follow the newest remote branch of the dashboard repository.

The tick polls the origin, takes the branch whose tip has the newest committer date, builds it in a
directory of its own named by the commit, health-checks a candidate instance on a scratch port, and
switches the service to it only when both pass. Any failure leaves the last good build serving and
is written to the status file as the refusal reason of the newest branch. All side effects sit behind
:class:`OmnidashHostPorts`, so the decision logic runs in memory.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from omnimarket.models.model_omnidash_host import (
    ModelOmnidashBranchTip,
    ModelOmnidashHostRow,
    ModelOmnidashHostSchedule,
    ModelOmnidashHostStatus,
    ModelOmnidashHostUpdateCommand,
    ModelOmnidashHostUpdateResult,
)

Clock = Callable[[], datetime]


class OmnidashPollError(RuntimeError):
    """The origin could not be read."""


class OmnidashHostPorts(Protocol):
    """Every side effect of a tick; each returns ``None`` on success or the refusal reason."""

    def list_branches(
        self, host: ModelOmnidashHostRow, schedule: ModelOmnidashHostSchedule
    ) -> Sequence[ModelOmnidashBranchTip]: ...

    def resolve_commit(
        self,
        host: ModelOmnidashHostRow,
        schedule: ModelOmnidashHostSchedule,
        sha: str,
    ) -> ModelOmnidashBranchTip | None: ...

    def build(
        self,
        host: ModelOmnidashHostRow,
        schedule: ModelOmnidashHostSchedule,
        sha: str,
    ) -> str | None: ...

    def check_health(
        self,
        host: ModelOmnidashHostRow,
        schedule: ModelOmnidashHostSchedule,
        sha: str,
    ) -> str | None: ...

    def switch(
        self,
        host: ModelOmnidashHostRow,
        schedule: ModelOmnidashHostSchedule,
        sha: str,
    ) -> str | None: ...

    def prune(
        self,
        host: ModelOmnidashHostRow,
        schedule: ModelOmnidashHostSchedule,
        keep: frozenset[str],
    ) -> None: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def newest_branch(
    tips: Sequence[ModelOmnidashBranchTip],
) -> ModelOmnidashBranchTip | None:
    """The tip with the newest committer date; a tie goes to the branch name that sorts last."""
    if not tips:
        return None
    return max(tips, key=lambda tip: (tip.committed_at, tip.branch))


def read_status(path: Path) -> ModelOmnidashHostStatus:
    if not path.is_file():
        return ModelOmnidashHostStatus()
    try:
        return ModelOmnidashHostStatus.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except ValueError:
        return ModelOmnidashHostStatus()


def write_status(path: Path, status: ModelOmnidashHostStatus) -> None:
    """Write the status file atomically so a reader never sees half a document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    scratch.write_text(
        json.dumps(status.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    scratch.replace(path)


class HandlerOmnidashHostEffect:
    """handle(ModelOmnidashHostUpdateCommand) -> ModelOmnidashHostUpdateResult."""

    def __init__(self, ports: OmnidashHostPorts, *, clock: Clock = utc_now) -> None:
        self._ports = ports
        self._clock = clock

    def handle(
        self, request: ModelOmnidashHostUpdateCommand
    ) -> ModelOmnidashHostUpdateResult:
        host, schedule = request.host, request.schedule
        status_path = Path(host.status_path)
        previous = read_status(status_path)
        polled_at = _stamp(self._clock())
        base = previous.model_copy(update={"last_poll_at": polled_at})

        try:
            tips = self._ports.list_branches(host, schedule)
            if request.pin_sha is not None:
                pinned = self._ports.resolve_commit(host, schedule, request.pin_sha)
                tips = (pinned,) if pinned is not None else ()
        except OmnidashPollError as exc:
            status = base.model_copy(
                update={"last_result": "poll-failed", "refusal_reason": str(exc)}
            )
            write_status(status_path, status)
            return ModelOmnidashHostUpdateResult(
                outcome="poll-failed", status=status, detail=str(exc)
            )

        target = newest_branch(tips)
        if target is None:
            status = base.model_copy(update={"last_result": "no-branches"})
            write_status(status_path, status)
            return ModelOmnidashHostUpdateResult(outcome="no-branches", status=status)

        seen = base.model_copy(
            update={"newest_branch": target.branch, "newest_sha": target.sha}
        )
        if target.sha == previous.served_sha:
            status = seen.model_copy(
                update={
                    "last_result": "unchanged",
                    "refusal_reason": None,
                    "refused_sha": None,
                    "served_branch": target.branch,
                }
            )
            write_status(status_path, status)
            return ModelOmnidashHostUpdateResult(outcome="unchanged", status=status)

        if target.sha == previous.refused_sha:
            status = seen.model_copy(update={"last_result": "refused-already"})
            write_status(status_path, status)
            return ModelOmnidashHostUpdateResult(
                outcome="refused-already",
                status=status,
                detail=status.refusal_reason or "",
            )

        for step, outcome in (
            (self._ports.build, "refused-build"),
            (self._ports.check_health, "refused-health"),
            (self._ports.switch, "refused-health"),
        ):
            reason = step(host, schedule, target.sha)
            if reason is not None:
                status = seen.model_copy(
                    update={
                        "last_result": outcome,
                        "refusal_reason": reason,
                        "refused_sha": target.sha,
                    }
                )
                write_status(status_path, status)
                return ModelOmnidashHostUpdateResult(
                    outcome=outcome,
                    status=status,
                    detail=reason,
                )

        switched = seen.model_copy(
            update={
                "served_branch": target.branch,
                "served_sha": target.sha,
                "served_committed_at": target.committed_at,
                "switched_at": _stamp(self._clock()),
                "last_result": "switched",
                "refusal_reason": None,
                "refused_sha": None,
            }
        )
        write_status(status_path, switched)
        keep = {target.sha}
        if previous.served_sha:
            keep.add(previous.served_sha)
        self._ports.prune(host, schedule, frozenset(keep))
        return ModelOmnidashHostUpdateResult(outcome="switched", status=switched)
