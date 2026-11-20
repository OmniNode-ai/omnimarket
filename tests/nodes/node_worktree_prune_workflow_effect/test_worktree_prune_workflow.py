# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The worktree prune workflow: eligibility, one activity per host with retries and resume, and the disk alarm."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.events.worktree_reconcile import (
    EnumWorktreeKind,
    EnumWorktreeReconcileDecision,
    ModelWorktreeFacts,
    ModelWorktreeReconcileRequest,
)
from omnimarket.models.model_worktree_reconcile_policy import (
    ModelWorktreeReconcilePolicy,
)
from omnimarket.nodes.node_worktree_prune_workflow_effect.handlers.handler_prune_eligibility import (
    HandlerWorktreePruneEligibility,
    removable_paths,
)
from omnimarket.nodes.node_worktree_prune_workflow_effect.handlers.handler_worktree_prune_run import (
    HandlerWorktreePruneRun,
    disk_alarm,
)
from omnimarket.nodes.node_worktree_prune_workflow_effect.models.model_worktree_prune_workflow import (
    ModelHostPruneReportRow,
    ModelHostPruneResult,
    ModelKeptTree,
    ModelPrunedTree,
    ModelPruneHostTarget,
    ModelWorktreePrunePlan,
)

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_worktree_prune_workflow_effect"
)


def _facts(name: str, **overrides: object) -> ModelWorktreeFacts:
    base: dict[str, object] = {
        "host": "h201",
        "path": f"/omni_worktrees/{name}/omnimarket",
        "root": "/omni_worktrees",
        "kind": EnumWorktreeKind.LINKED_WORKTREE,
        "branch": f"omn-1-{name}",
        "head_sha": "a" * 40,
        "has_remote": True,
        "head_on_remote": True,
        "commits_not_on_remote": 0,
        "content_merged": True,
        "last_activity_age_hours": 100.0,
    }
    base.update(overrides)
    return ModelWorktreeFacts.model_validate(base)


# AC1 -------------------------------------------------------------------------------------------------------------


def test_eligibility_removes_only_clean_merged_unclaimed_trees() -> None:
    request = ModelWorktreeReconcileRequest(
        facts=(
            _facts("clean-merged"),
            _facts("dirty", dirty_tracked_count=2),
            _facts(
                "unpushed",
                content_merged=False,
                head_on_remote=False,
                commits_not_on_remote=3,
            ),
            _facts("claimed", live_claim=True),
            _facts("pushed-unmerged", content_merged=False),
        ),
        policy=ModelWorktreeReconcilePolicy(merged_only=False),
    )
    decisions = HandlerWorktreePruneEligibility().handle(request)

    assert removable_paths(decisions) == ("/omni_worktrees/clean-merged/omnimarket",)
    by_path = {row.path.split("/")[2]: row for row in decisions.decisions}
    assert by_path["clean-merged"].decision == EnumWorktreeReconcileDecision.REMOVE
    for name in ("dirty", "unpushed", "claimed", "pushed-unmerged"):
        assert by_path[name].decision not in (
            EnumWorktreeReconcileDecision.REMOVE,
            EnumWorktreeReconcileDecision.PIN_AND_REMOVE,
        ), name
    assert by_path["claimed"].reasons == ("live_claim",)
    assert by_path["dirty"].reasons == ("dirty",)
    # The merged-only rule holds the unpushed tree even past the stale bar, where the plain classifier would pin it.
    assert by_path["unpushed"].reasons == ("merged_only:unpushed_work_stale",)
    assert decisions.policy.merged_only is True


# AC2 -------------------------------------------------------------------------------------------------------------


def _plan(*hosts: str, attempts: int = 3) -> ModelWorktreePrunePlan:
    return ModelWorktreePrunePlan(
        run_id="run-1",
        trigger="schedule",
        hosts=tuple(
            ModelPruneHostTarget(host=host, disk_bar_percent=85.0) for host in hosts
        ),
        max_attempts=attempts,
    )


class _Activity:
    """Records each call; fails a host for its first ``fail_first`` attempts."""

    def __init__(self, fail_first: dict[str, int] | None = None) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.fail_first = dict(fail_first or {})

    def __call__(
        self, target: ModelPruneHostTarget, step_key: str, attempt: int
    ) -> ModelHostPruneResult:
        self.calls.append((target.host, step_key, attempt))
        if attempt <= self.fail_first.get(target.host, 0):
            raise ConnectionError(f"ssh {target.host} timed out")
        return ModelHostPruneResult(
            host=target.host,
            disk_used_percent_before=80.0,
            disk_used_percent_after=70.0,
            removed=(ModelPrunedTree(path=f"/{target.host}/a", freed_bytes=1024),),
            kept=(ModelKeptTree(path=f"/{target.host}/b", reason="live_claim"),),
        )


def test_one_activity_per_host_and_a_row_per_host() -> None:
    activity = _Activity()
    report = HandlerWorktreePruneRun(activity).handle(_plan("h201", "h202", "h051"))

    assert [call[0] for call in activity.calls] == ["h201", "h202", "h051"]
    assert [call[1] for call in activity.calls] == [
        "run-1/h201",
        "run-1/h202",
        "run-1/h051",
    ]
    assert [row.host for row in report.rows] == ["h201", "h202", "h051"]
    assert all(row.status == "ok" for row in report.rows)
    assert report.freed_bytes == 3 * 1024


def test_failed_host_is_retried_then_resumed_without_rerunning_finished_hosts() -> None:
    journal: dict[str, ModelHostPruneReportRow] = {}
    first = _Activity(fail_first={"h202": 5})
    report = HandlerWorktreePruneRun(first, journal).handle(
        _plan("h201", "h202", "h053", attempts=2)
    )
    h202 = [call for call in first.calls if call[0] == "h202"]
    assert [call[2] for call in h202] == [1, 2]
    assert [(row.host, row.status) for row in report.rows] == [
        ("h201", "ok"),
        ("h202", "failed"),
        ("h053", "ok"),
    ]
    assert "ssh h202 timed out" in (report.rows[1].error or "")

    resumed = _Activity()
    again = HandlerWorktreePruneRun(resumed, journal).handle(
        _plan("h201", "h202", "h053", attempts=2)
    )
    assert [call[0] for call in resumed.calls] == ["h202"]
    assert [(row.host, row.status) for row in again.rows] == [
        ("h201", "ok"),
        ("h202", "ok"),
        ("h053", "ok"),
    ]


def test_a_retry_that_succeeds_reads_ok_with_its_attempt_count() -> None:
    activity = _Activity(fail_first={"h201": 1})
    report = HandlerWorktreePruneRun(activity).handle(_plan("h201"))
    assert report.rows[0].status == "ok"
    assert report.rows[0].attempts == 2


def test_plan_refuses_a_host_named_twice() -> None:
    with pytest.raises(ValueError, match="named twice"):
        _plan("h201", "h201")


# AC3 -------------------------------------------------------------------------------------------------------------


def test_disk_alarm_fires_past_the_bar_and_stays_quiet_at_or_below_it() -> None:
    target = ModelPruneHostTarget(host="h201", disk_bar_percent=85.0)
    alarm = disk_alarm(target, 91.5)
    assert alarm is not None
    assert (alarm.host, alarm.used_percent, alarm.bar_percent) == ("h201", 91.5, 85.0)
    assert disk_alarm(target, 85.0) is None
    assert disk_alarm(target, 40.0) is None


def test_run_report_carries_the_alarm_of_the_host_past_its_bar() -> None:
    class _Full(_Activity):
        def __call__(
            self, target: ModelPruneHostTarget, step_key: str, attempt: int
        ) -> ModelHostPruneResult:
            result = super().__call__(target, step_key, attempt)
            if target.host == "h105":
                return result.model_copy(update={"disk_used_percent_after": 99.0})
            return result

    report = HandlerWorktreePruneRun(_Full()).handle(_plan("h201", "h105"))
    assert [alarm.host for alarm in report.alarms] == ["h105"]
    assert report.rows[0].alarm is None


# Contract ----------------------------------------------------------------------------------------------------------


def test_contract_declares_the_daily_schedule_the_close_trigger_and_the_alarm_topic() -> (
    None
):
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["schedule"]["cron"] == "17 6 * * *"
    assert (
        "onex.evt.omnimarket.ticket-closer-result.v1"
        in contract["event_bus"]["subscribe_topics"]
    )
    assert (
        "onex.evt.omnimarket.worktree-prune-disk-alarm.v1"
        in contract["event_bus"]["publish_topics"]
    )
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    for topic in (
        "onex.evt.omnimarket.worktree-prune-host-reported.v1",
        "onex.evt.omnimarket.worktree-prune-run-completed.v1",
    ):
        assert topic in contract["event_bus"]["publish_topics"]
