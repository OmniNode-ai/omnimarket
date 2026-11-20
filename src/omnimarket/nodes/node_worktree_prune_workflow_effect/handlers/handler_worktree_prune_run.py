# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# unimported-handler-allow: HandlerWorktreePruneRun is resolved at boot from the contract handler block and handler_routing, by module path, not by a Python import
"""The body of one worktree prune run: one host activity per host, retries, resume and a per-host report row.

The host activity is injected (``ProtocolHostPruneActivity``): on a lab host it runs the declared pruner on that host
and returns its disk readings, removals and keeps. The journal holds every finished host's row under its step key
``<run_id>/<host>``; a run started again with the same journal reuses those rows and runs only the hosts that have
none, so a finished host is never pruned twice in one run. A host whose activity still fails after the plan's attempt
limit yields a failed row and the run goes on to the next host.
"""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import Any, Protocol

import yaml

from omnimarket.nodes.node_worktree_prune_workflow_effect.models.model_worktree_prune_workflow import (
    ModelDiskAlarm,
    ModelHostPruneReportRow,
    ModelHostPruneResult,
    ModelPruneHostTarget,
    ModelWorktreePrunePlan,
    ModelWorktreePruneRunReport,
)

_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
_HOST_REPORTED_INDEX = 0
_DISK_ALARM_INDEX = 1


def _declared_topics() -> tuple[str, str]:
    """The host-reported and disk-alarm topics, read from this node's contract.yaml publish list."""
    raw = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    topics = raw["event_bus"]["publish_topics"]
    return str(topics[_HOST_REPORTED_INDEX]), str(topics[_DISK_ALARM_INDEX])


class ProtocolHostPruneActivity(Protocol):
    def __call__(
        self, target: ModelPruneHostTarget, step_key: str, attempt: int
    ) -> ModelHostPruneResult: ...


def disk_alarm(
    target: ModelPruneHostTarget, used_percent: float
) -> ModelDiskAlarm | None:
    """An alarm when the use is past the bar, none at or below it."""
    if used_percent > target.disk_bar_percent:
        return ModelDiskAlarm(
            host=target.host,
            used_percent=used_percent,
            bar_percent=target.disk_bar_percent,
        )
    return None


def _unconfigured_activity(
    target: ModelPruneHostTarget, step_key: str, attempt: int
) -> ModelHostPruneResult:
    raise RuntimeError("no host prune activity is configured for this runtime")


def step_key(plan: ModelWorktreePrunePlan, host: str) -> str:
    return f"{plan.run_id}/{host}"


class HandlerWorktreePruneRun:
    def __init__(
        self,
        activity: ProtocolHostPruneActivity = _unconfigured_activity,
        journal: MutableMapping[str, ModelHostPruneReportRow] | None = None,
        publish: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._activity = activity
        self._publish = publish
        self.journal: MutableMapping[str, ModelHostPruneReportRow] = (
            journal if journal is not None else {}
        )

    def _run_host(
        self, plan: ModelWorktreePrunePlan, target: ModelPruneHostTarget
    ) -> ModelHostPruneReportRow:
        key = step_key(plan, target.host)
        error = ""
        for attempt in range(1, plan.max_attempts + 1):
            try:
                result = self._activity(target, key, attempt)
            except (
                Exception
            ) as exc:  # every host failure becomes a retry, then a failed row
                error = f"{type(exc).__name__}: {exc}"
                continue
            if result.host != target.host:
                error = f"activity for {target.host} answered for {result.host}"
                continue
            return ModelHostPruneReportRow(
                host=target.host,
                status="ok",
                attempts=attempt,
                result=result,
                alarm=disk_alarm(target, result.disk_used_percent_after),
            )
        return ModelHostPruneReportRow(
            host=target.host,
            status="failed",
            attempts=plan.max_attempts,
            error=error,
        )

    def _publish_row(
        self, plan: ModelWorktreePrunePlan, row: ModelHostPruneReportRow
    ) -> None:
        """Publish the host's report row, and its disk alarm when it has one, through the injected publisher."""
        if self._publish is None:
            return
        host_reported, disk_alarm_topic = _declared_topics()
        self._publish(
            host_reported, {"run_id": plan.run_id, **row.model_dump(mode="json")}
        )
        if row.alarm is not None:
            self._publish(
                disk_alarm_topic,
                {"run_id": plan.run_id, **row.alarm.model_dump(mode="json")},
            )

    def handle(self, request: ModelWorktreePrunePlan) -> ModelWorktreePruneRunReport:
        rows = []
        for target in request.hosts:
            key = step_key(request, target.host)
            done = self.journal.get(key)
            if done is None or done.status != "ok":
                done = self._run_host(request, target)
                self.journal[key] = done
                self._publish_row(request, done)
            rows.append(done)
        return ModelWorktreePruneRunReport(
            run_id=request.run_id, trigger=request.trigger, rows=tuple(rows)
        )


__all__ = [
    "HandlerWorktreePruneRun",
    "ProtocolHostPruneActivity",
    "disk_alarm",
    "step_key",
]
