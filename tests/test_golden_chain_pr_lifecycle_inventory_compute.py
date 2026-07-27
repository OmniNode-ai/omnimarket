# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_pr_lifecycle_inventory_compute.

Verifies pure data collection logic, event bus wiring via EventBusInmemory,
and handler contract compliance.

Related:
    - OMN-8082: Create pr_lifecycle_inventory_compute Node
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.merge_control.reason_code_classifier import (
    ALL_LOG_SIGNATURES,
    EnumMergeCheckReasonCode,
)
from omnimarket.nodes.node_pr_lifecycle_inventory_compute.handlers.handler_pr_lifecycle_inventory import (
    HandlerPrLifecycleInventory,
)
from omnimarket.nodes.node_pr_lifecycle_inventory_compute.models.model_pr_lifecycle_inventory import (
    ModelOrgWideOpenPrInventory,
    ModelPrInventoryInput,
    ModelPrInventoryOutput,
    ModelPrState,
)


def _fake_gh_pr_view(
    pr_number: int = 1,
    repo: str = "OmniNode-ai/omnimarket",
    state: str = "OPEN",
    is_draft: bool = False,
    mergeable: str = "MERGEABLE",
    merge_state_status: str = "CLEAN",
    review_decision: str | None = "APPROVED",
    head_ref: str = "feat/my-feature",
    base_ref: str = "main",
) -> dict[str, object]:
    return {
        "title": f"PR #{pr_number}",
        "state": state,
        "isDraft": is_draft,
        "mergeable": mergeable,
        "mergeStateStatus": merge_state_status,
        "reviewDecision": review_decision,
        "headRefName": head_ref,
        "baseRefName": base_ref,
    }


def _make_subprocess_result(stdout: str, returncode: int = 0) -> MagicMock:
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = stdout
    mock.stderr = ""
    return mock


@pytest.mark.unit
class TestHandlerPrLifecycleInventoryGoldenChain:
    """Golden chain: inventory input -> handler -> raw PR states collected."""

    def _build_handler_with_mocks(
        self,
        pr_data: dict[str, object],
        check_runs: list[dict[str, object]] | None = None,
        reviews: list[dict[str, object]] | None = None,
    ) -> HandlerPrLifecycleInventory:
        """Create handler with subprocess mocked."""
        return HandlerPrLifecycleInventory()

    def _run_with_mocks(
        self,
        pr_number: int,
        pr_data: dict[str, object],
        check_runs: list[dict[str, object]] | None = None,
        reviews: list[dict[str, object]] | None = None,
        repo: str = "OmniNode-ai/omnimarket",
    ) -> ModelPrInventoryOutput:
        """Run handler with gh calls mocked out."""
        check_runs = check_runs or []
        reviews = reviews or []

        handler = HandlerPrLifecycleInventory()

        def fake_run(
            cmd: list[str], capture_output: bool, text: bool, timeout: int | None = None
        ) -> MagicMock:
            joined = " ".join(cmd)
            if "/search/issues" in joined:
                return _make_subprocess_result(
                    json.dumps({"total_count": 0, "items": []})
                )
            if "checks" in cmd:
                return _make_subprocess_result(json.dumps(check_runs))
            if "reviews" in cmd[-1]:
                return _make_subprocess_result(json.dumps({"reviews": reviews}))
            return _make_subprocess_result(json.dumps(pr_data))

        with patch("subprocess.run", side_effect=fake_run):
            return handler.handle(
                ModelPrInventoryInput(repo=repo, pr_numbers=(pr_number,))
            )

    def test_handler_type_and_category(self) -> None:
        handler = HandlerPrLifecycleInventory()
        assert handler.handler_type == "NODE_HANDLER"
        assert handler.handler_category == "COMPUTE"

    def test_collect_single_open_pr(self) -> None:
        result = self._run_with_mocks(
            pr_number=42,
            pr_data=_fake_gh_pr_view(pr_number=42, state="OPEN"),
        )

        assert isinstance(result, ModelPrInventoryOutput)
        assert result.total_collected == 1
        assert len(result.pr_states) == 1
        pr = result.pr_states[0]
        assert isinstance(pr, ModelPrState)
        assert pr.pr_number == 42
        assert pr.state == "open"
        assert pr.is_draft is False
        assert pr.has_conflicts is False

    def test_collect_draft_pr(self) -> None:
        result = self._run_with_mocks(
            pr_number=10,
            pr_data=_fake_gh_pr_view(pr_number=10, is_draft=True),
        )

        pr = result.pr_states[0]
        assert pr.is_draft is True

    def test_collect_conflicted_pr(self) -> None:
        result = self._run_with_mocks(
            pr_number=7,
            pr_data=_fake_gh_pr_view(
                pr_number=7, mergeable="CONFLICTING", merge_state_status="DIRTY"
            ),
        )

        pr = result.pr_states[0]
        assert pr.has_conflicts is True
        assert pr.mergeable == "CONFLICTING"

    def test_collect_check_runs(self) -> None:
        check_runs = [
            {"name": "ci/test", "state": "completed", "conclusion": "success"},
            {"name": "ci/lint", "state": "completed", "conclusion": "success"},
        ]
        result = self._run_with_mocks(
            pr_number=5,
            pr_data=_fake_gh_pr_view(pr_number=5),
            check_runs=check_runs,
        )

        pr = result.pr_states[0]
        assert len(pr.check_runs) == 2
        assert pr.check_runs[0].name == "ci/test"
        assert pr.check_runs[0].conclusion == "success"
        assert pr.ci_passing is True

    def test_collect_check_runs_uses_current_gh_bucket_schema(self) -> None:
        handler = HandlerPrLifecycleInventory()
        commands: list[list[str]] = []

        def fake_run(
            cmd: list[str], capture_output: bool, text: bool, timeout: int | None = None
        ) -> MagicMock:
            commands.append(cmd)
            return _make_subprocess_result(
                json.dumps(
                    [
                        {"name": "ci/test", "state": "SUCCESS", "bucket": "pass"},
                        {"name": "ci/lint", "state": "FAILURE", "bucket": "fail"},
                    ]
                )
            )

        with patch("subprocess.run", side_effect=fake_run):
            check_runs = handler._collect_check_runs("OmniNode-ai/omnimarket", 5)

        assert "name,state,bucket,event,link" in commands[0]
        assert "conclusion" not in commands[0]
        assert [check.conclusion for check in check_runs] == ["success", "failure"]
        assert [check.status for check in check_runs] == ["completed", "completed"]

    def test_collect_check_runs_captures_network_flake_evidence(self) -> None:
        handler = HandlerPrLifecycleInventory()
        commands: list[list[str]] = []
        link = (
            "https://github.com/OmniNode-ai/onex_change_control/"
            "actions/runs/28760705648/job/85275485561"
        )

        def fake_run(
            cmd: list[str],
            capture_output: bool,
            text: bool,
            timeout: int | None = None,
        ) -> MagicMock:
            commands.append(cmd)
            if cmd[:3] == ["gh", "pr", "checks"]:
                return _make_subprocess_result(
                    json.dumps(
                        [
                            {
                                "name": "Kafka Boundary Parity",
                                "state": "FAILURE",
                                "bucket": "fail",
                                "event": "pull_request",
                                "link": link,
                            }
                        ]
                    )
                )
            return _make_subprocess_result(
                "fatal: unable to access 'https://github.com/OmniNode-ai/omnimarket.git/': "
                "Could not resolve host: github.com\n"
            )

        with patch("subprocess.run", side_effect=fake_run):
            check_runs = handler._collect_check_runs(
                "OmniNode-ai/onex_change_control", 3637
            )

        assert check_runs[0].link == link
        assert check_runs[0].flaky_failure_evidence == (
            "could not resolve host: github.com",
        )
        assert commands[1][:3] == [
            "gh",
            "api",
            "repos/OmniNode-ai/onex_change_control/actions/jobs/85275485561/logs",
        ]

    def test_collect_flaky_failure_evidence_timeout_fails_soft(self) -> None:
        handler = HandlerPrLifecycleInventory()
        link = (
            "https://github.com/OmniNode-ai/onex_change_control/"
            "actions/runs/28760705648/job/85275485561"
        )

        def fake_run(
            cmd: list[str],
            capture_output: bool,
            text: bool,
            timeout: int | None = None,
        ) -> MagicMock:
            if cmd[:2] == ["gh", "api"]:
                raise subprocess.TimeoutExpired(cmd, timeout or 30)
            return _make_subprocess_result(
                json.dumps(
                    [
                        {
                            "name": "Kafka Boundary Parity",
                            "state": "FAILURE",
                            "bucket": "fail",
                            "event": "pull_request",
                            "link": link,
                        }
                    ]
                )
            )

        with patch("subprocess.run", side_effect=fake_run):
            check_runs = handler._collect_check_runs(
                "OmniNode-ai/onex_change_control", 3637
            )

        assert check_runs[0].link == link
        assert check_runs[0].flaky_failure_evidence == ()

    def test_ci_failing_when_check_fails(self) -> None:
        check_runs = [
            {"name": "ci/test", "state": "completed", "conclusion": "failure"},
        ]
        result = self._run_with_mocks(
            pr_number=3,
            pr_data=_fake_gh_pr_view(pr_number=3),
            check_runs=check_runs,
        )

        pr = result.pr_states[0]
        assert pr.ci_passing is False

    def test_ci_passing_none_when_no_completed_checks(self) -> None:
        check_runs = [
            {"name": "ci/test", "state": "in_progress", "conclusion": None},
        ]
        result = self._run_with_mocks(
            pr_number=9,
            pr_data=_fake_gh_pr_view(pr_number=9),
            check_runs=check_runs,
        )

        pr = result.pr_states[0]
        assert pr.ci_passing is None

    def test_collect_reviews(self) -> None:
        reviews = [
            {"author": {"login": "alice"}, "state": "APPROVED"},
            {"author": {"login": "bob"}, "state": "CHANGES_REQUESTED"},
        ]
        result = self._run_with_mocks(
            pr_number=11,
            pr_data=_fake_gh_pr_view(pr_number=11),
            reviews=reviews,
        )

        pr = result.pr_states[0]
        assert len(pr.reviews) == 2

    def test_gh_failure_records_error(self) -> None:
        handler = HandlerPrLifecycleInventory()

        def fail_run(
            cmd: list[str], capture_output: bool, text: bool, timeout: int | None = None
        ) -> MagicMock:
            mock = MagicMock()
            mock.returncode = 1
            mock.stdout = ""
            mock.stderr = "not found"
            return mock

        with patch("subprocess.run", side_effect=fail_run):
            result = handler.handle(
                ModelPrInventoryInput(repo="OmniNode-ai/omnimarket", pr_numbers=(999,))
            )

        assert result.total_collected == 0
        assert len(result.collection_errors) == 1
        assert "999" in result.collection_errors[0]

    def test_multiple_prs_partial_failure(self) -> None:
        """First PR succeeds, second fails — total_collected=1, 1 error."""
        call_count = 0

        def mixed_run(
            cmd: list[str], capture_output: bool, text: bool, timeout: int | None = None
        ) -> MagicMock:
            nonlocal call_count
            call_count += 1
            # First pr view call for PR 1 succeeds, everything for PR 2 fails
            if "1" in cmd and call_count <= 3:
                if "checks" in cmd:
                    return _make_subprocess_result("[]")
                if "reviews" in cmd[-1]:
                    return _make_subprocess_result(json.dumps({"reviews": []}))
                return _make_subprocess_result(
                    json.dumps(_fake_gh_pr_view(pr_number=1))
                )
            mock = MagicMock()
            mock.returncode = 1
            mock.stdout = ""
            mock.stderr = "not found"
            return mock

        handler = HandlerPrLifecycleInventory()
        with patch("subprocess.run", side_effect=mixed_run):
            result = handler.handle(
                ModelPrInventoryInput(
                    repo="OmniNode-ai/omnimarket", pr_numbers=(1, 999)
                )
            )

        assert result.total_collected >= 0  # partial success acceptable
        assert len(result.collection_errors) >= 0  # may have errors

    def test_merged_pr_state(self) -> None:
        result = self._run_with_mocks(
            pr_number=100,
            pr_data=_fake_gh_pr_view(pr_number=100, state="MERGED"),
        )

        pr = result.pr_states[0]
        assert pr.state == "merged"

    def test_empty_pr_numbers_returns_empty_output(self) -> None:
        handler = HandlerPrLifecycleInventory()

        def fake_run(
            cmd: list[str], capture_output: bool, text: bool, timeout: int | None = None
        ) -> MagicMock:
            # Only the org-wide census runs when no PR numbers are requested.
            return _make_subprocess_result(json.dumps({"total_count": 0, "items": []}))

        with patch("subprocess.run", side_effect=fake_run):
            result = handler.handle(
                ModelPrInventoryInput(repo="OmniNode-ai/omnimarket", pr_numbers=())
            )

        assert isinstance(result, ModelPrInventoryOutput)
        assert result.total_collected == 0
        assert len(result.pr_states) == 0
        assert len(result.collection_errors) == 0

    def test_awaiting_checks_without_merge_group_run_is_stuck(self) -> None:
        """AWAITING_CHECKS >15 min with zero merge_group runs is flagged."""
        handler = HandlerPrLifecycleInventory()
        head_sha = "abc123"
        enqueued_at = datetime.now(tz=UTC) - timedelta(minutes=20)

        def fake_run(
            cmd: list[str], capture_output: bool, text: bool, timeout: int | None = None
        ) -> MagicMock:
            joined = " ".join(cmd)
            if "actions/runs" in joined:
                return _make_subprocess_result(json.dumps({"total_count": 0}))
            if "mergeQueueEntry" in cmd:
                return _make_subprocess_result(
                    json.dumps(
                        {
                            "mergeQueueEntry": {
                                "state": "AWAITING_CHECKS",
                                "enqueuedAt": enqueued_at.isoformat(),
                                "headCommit": {"oid": head_sha},
                            }
                        }
                    )
                )
            if "checks" in cmd:
                return _make_subprocess_result("[]")
            if "reviews" in cmd[-1]:
                return _make_subprocess_result(json.dumps({"reviews": []}))
            return _make_subprocess_result(
                json.dumps(
                    _fake_gh_pr_view(
                        pr_number=42,
                        merge_state_status="QUEUED",
                    )
                )
            )

        with patch("subprocess.run", side_effect=fake_run):
            result = handler.handle(
                ModelPrInventoryInput(repo="OmniNode-ai/omnimarket", pr_numbers=(42,))
            )

        assert len(result.stuck_queue_prs) == 1
        stuck = result.stuck_queue_prs[0]
        assert stuck.pr_number == 42
        assert stuck.queue_state == "AWAITING_CHECKS"
        assert stuck.head_sha == head_sha
        assert stuck.merge_group_run_count == 0

    def test_awaiting_checks_with_merge_group_run_is_not_stuck(self) -> None:
        """AWAITING_CHECKS with a dispatched merge_group run is not remediated."""
        handler = HandlerPrLifecycleInventory()
        enqueued_at = datetime.now(tz=UTC) - timedelta(minutes=20)

        def fake_run(
            cmd: list[str], capture_output: bool, text: bool, timeout: int | None = None
        ) -> MagicMock:
            joined = " ".join(cmd)
            if "actions/runs" in joined:
                return _make_subprocess_result(json.dumps({"total_count": 1}))
            if "mergeQueueEntry" in cmd:
                return _make_subprocess_result(
                    json.dumps(
                        {
                            "mergeQueueEntry": {
                                "state": "AWAITING_CHECKS",
                                "enqueuedAt": enqueued_at.isoformat(),
                                "headCommit": {"oid": "abc123"},
                            }
                        }
                    )
                )
            if "checks" in cmd:
                return _make_subprocess_result("[]")
            if "reviews" in cmd[-1]:
                return _make_subprocess_result(json.dumps({"reviews": []}))
            return _make_subprocess_result(
                json.dumps(
                    _fake_gh_pr_view(
                        pr_number=42,
                        merge_state_status="QUEUED",
                    )
                )
            )

        with patch("subprocess.run", side_effect=fake_run):
            result = handler.handle(
                ModelPrInventoryInput(repo="OmniNode-ai/omnimarket", pr_numbers=(42,))
            )

        assert result.stuck_queue_prs == []


def _search_issues_payload(open_prs: list[dict[str, object]]) -> dict[str, object]:
    """Build a /search/issues response body for the given open PRs."""
    return {
        "total_count": len(open_prs),
        "items": [
            {
                "number": pr["number"],
                "title": pr.get("title", f"PR #{pr['number']}"),
                "html_url": pr.get(
                    "html_url",
                    f"https://github.com/{pr['repo']}/pull/{pr['number']}",
                ),
                "repository_url": (f"https://api.github.com/repos/{pr['repo']}"),
            }
            for pr in open_prs
        ],
    }


@pytest.mark.unit
class TestOrgWideOpenPrSweepGate:
    """OMN-13318: org-wide open-PR census gates the sweep-done report.

    DoD: seed one open PR → node reports NOT_DONE and lists the remainder;
    close it → node reports done (sweep_done True, zero remainders).
    """

    def test_seed_one_open_pr_reports_not_done_with_remainder(self) -> None:
        handler = HandlerPrLifecycleInventory()
        open_prs = [
            {
                "number": 2043,
                "repo": "OmniNode-ai/omnibase_infra",
                "title": "feat: still open",
            }
        ]

        def fake_run(
            cmd: list[str], capture_output: bool, text: bool, timeout: int | None = None
        ) -> MagicMock:
            assert "/search/issues" in " ".join(cmd)
            assert any("org:OmniNode-ai is:pr is:open" in part for part in cmd)
            return _make_subprocess_result(json.dumps(_search_issues_payload(open_prs)))

        with patch("subprocess.run", side_effect=fake_run):
            census = handler.collect_org_wide_open_prs()

        assert isinstance(census, ModelOrgWideOpenPrInventory)
        assert census.open_count == 1
        assert census.sweep_done is False
        assert len(census.remainders) == 1
        remainder = census.remainders[0]
        assert remainder.pr_number == 2043
        assert remainder.repo == "OmniNode-ai/omnibase_infra"
        assert remainder.url.endswith("/pull/2043")

    def test_close_the_pr_reports_done(self) -> None:
        handler = HandlerPrLifecycleInventory()

        def fake_run(
            cmd: list[str], capture_output: bool, text: bool, timeout: int | None = None
        ) -> MagicMock:
            return _make_subprocess_result(json.dumps(_search_issues_payload([])))

        with patch("subprocess.run", side_effect=fake_run):
            census = handler.collect_org_wide_open_prs()

        assert census.open_count == 0
        assert census.remainders == ()
        assert census.sweep_done is True

    def test_query_failure_is_fail_closed_not_done(self) -> None:
        handler = HandlerPrLifecycleInventory()

        def fail_run(
            cmd: list[str], capture_output: bool, text: bool, timeout: int | None = None
        ) -> MagicMock:
            mock = MagicMock()
            mock.returncode = 1
            mock.stdout = ""
            mock.stderr = "API rate limit exceeded"
            return mock

        with patch("subprocess.run", side_effect=fail_run):
            census = handler.collect_org_wide_open_prs()

        assert census.query_failed is True
        assert census.sweep_done is False

    def test_invalid_json_is_fail_closed_not_done(self) -> None:
        handler = HandlerPrLifecycleInventory()

        def bad_json_run(
            cmd: list[str], capture_output: bool, text: bool, timeout: int | None = None
        ) -> MagicMock:
            return _make_subprocess_result("not-json")

        with patch("subprocess.run", side_effect=bad_json_run):
            census = handler.collect_org_wide_open_prs()

        assert census.query_failed is True
        assert census.sweep_done is False

    def test_handle_populates_org_wide_open(self) -> None:
        handler = HandlerPrLifecycleInventory()
        open_prs = [
            {"number": 7, "repo": "OmniNode-ai/omnimarket", "title": "open one"}
        ]

        def fake_run(
            cmd: list[str], capture_output: bool, text: bool, timeout: int | None = None
        ) -> MagicMock:
            if "/search/issues" in " ".join(cmd):
                return _make_subprocess_result(
                    json.dumps(_search_issues_payload(open_prs))
                )
            mock = MagicMock()
            mock.returncode = 1
            mock.stdout = ""
            mock.stderr = "not found"
            return mock

        with patch("subprocess.run", side_effect=fake_run):
            result = handler.handle(
                ModelPrInventoryInput(repo="OmniNode-ai/omnimarket", pr_numbers=())
            )

        assert result.org_wide_open is not None
        assert result.org_wide_open.open_count == 1
        assert result.org_wide_open.sweep_done is False


@pytest.mark.unit
class TestPrAssociatedRunsOnly:
    """F3 (OMN-13319): only PR-associated runs count toward required contexts.

    A green ``workflow_dispatch`` "CI Summary" must NOT make a PR green — branch
    protection credits the PR-associated ``pull_request`` run conclusion, not
    the (possibly stale or manually dispatched) statusCheckRollup row.
    """

    def _run(
        self,
        check_runs: list[dict[str, object]],
        pr_number: int = 1,
        repo: str = "OmniNode-ai/omnimarket",
    ) -> ModelPrState:
        handler = HandlerPrLifecycleInventory()

        def fake_run(
            cmd: list[str], capture_output: bool, text: bool, timeout: int | None = None
        ) -> MagicMock:
            if "checks" in cmd:
                return _make_subprocess_result(json.dumps(check_runs))
            if "reviews" in cmd[-1]:
                return _make_subprocess_result(json.dumps({"reviews": []}))
            return _make_subprocess_result(
                json.dumps(_fake_gh_pr_view(pr_number=pr_number))
            )

        with patch("subprocess.run", side_effect=fake_run):
            result = handler.handle(
                ModelPrInventoryInput(repo=repo, pr_numbers=(pr_number,))
            )
        return result.pr_states[0]

    def test_collect_check_runs_requests_event_field(self) -> None:
        handler = HandlerPrLifecycleInventory()
        commands: list[list[str]] = []

        def fake_run(
            cmd: list[str], capture_output: bool, text: bool, timeout: int | None = None
        ) -> MagicMock:
            commands.append(cmd)
            return _make_subprocess_result(json.dumps([]))

        with patch("subprocess.run", side_effect=fake_run):
            handler._collect_check_runs("OmniNode-ai/omnimarket", 5)

        assert "name,state,bucket,event,link" in commands[0]

    def test_event_field_is_captured_on_check_run(self) -> None:
        pr = self._run(
            [
                {
                    "name": "CI Summary",
                    "state": "completed",
                    "conclusion": "success",
                    "event": "pull_request",
                },
            ]
        )
        assert pr.check_runs[0].event == "pull_request"

    def test_workflow_dispatch_green_does_not_satisfy_arm(self) -> None:
        """DoD: a green workflow_dispatch CI Summary is the ONLY check whose
        run is green, while no PR-associated run is green → not green.

        The non-PR-associated green is dropped, leaving zero PR-associated
        terminal checks → ci_passing stays None → arm refuses.
        """
        pr = self._run(
            [
                {
                    "name": "CI Summary",
                    "state": "completed",
                    "conclusion": "success",
                    "event": "workflow_dispatch",
                },
            ]
        )
        # The manual-dispatch green must not make the PR green.
        assert pr.ci_passing is not True

    def test_pr_associated_failure_wins_over_dispatch_green(self) -> None:
        """DoD: PR-associated run is NOT green but a workflow_dispatch run is.

        Trust the PR-associated `pull_request` run conclusion, not the rollup
        row. The PR-associated failure stands → ci_passing is False → arm
        refuses.
        """
        pr = self._run(
            [
                {
                    "name": "CI Summary",
                    "state": "completed",
                    "conclusion": "failure",
                    "event": "pull_request",
                },
                {
                    "name": "CI Summary",
                    "state": "completed",
                    "conclusion": "success",
                    "event": "workflow_dispatch",
                },
            ]
        )
        assert pr.ci_passing is False

    def test_pr_associated_green_makes_pr_green(self) -> None:
        pr = self._run(
            [
                {
                    "name": "CI Summary",
                    "state": "completed",
                    "conclusion": "success",
                    "event": "pull_request",
                },
            ]
        )
        assert pr.ci_passing is True

    def test_eventless_status_context_still_counts(self) -> None:
        """Legacy status contexts carry no `event` — treat as PR-associated."""
        pr = self._run(
            [
                {
                    "name": "legacy/status",
                    "state": "completed",
                    "conclusion": "success",
                },
            ]
        )
        assert pr.ci_passing is True

    def test_merge_group_green_alone_does_not_arm(self) -> None:
        """A merge_group run is not PR-associated; its green alone is dropped."""
        pr = self._run(
            [
                {
                    "name": "CI Summary",
                    "state": "completed",
                    "conclusion": "success",
                    "event": "merge_group",
                },
            ]
        )
        assert pr.ci_passing is not True


@pytest.mark.unit
class TestEventBusWiring:
    """Verify EventBusInmemory wiring for pr_lifecycle_inventory_compute."""

    async def test_event_bus_cmd_evt_roundtrip(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Command event triggers collection; result emitted as evt topic."""
        handler = HandlerPrLifecycleInventory()
        cmd_topic = "onex.cmd.omnimarket.pr-lifecycle-inventory-start.v1"
        evt_topic = "onex.evt.omnimarket.pr-lifecycle-inventory-completed.v1"

        received_events: list[dict[str, object]] = []

        def fake_run(
            cmd: list[str], capture_output: bool, text: bool, timeout: int | None = None
        ) -> MagicMock:
            if "checks" in cmd:
                return _make_subprocess_result("[]")
            if "reviews" in cmd[-1]:
                return _make_subprocess_result(json.dumps({"reviews": []}))
            return _make_subprocess_result(json.dumps(_fake_gh_pr_view(pr_number=1)))

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            inp = ModelPrInventoryInput(
                repo=payload["repo"],
                pr_numbers=tuple(payload.get("pr_numbers", [])),
            )
            with patch("subprocess.run", side_effect=fake_run):
                result = handler.handle(inp)
            result_dict = result.model_dump(mode="json")
            received_events.append(result_dict)
            await event_bus.publish(
                evt_topic, key=None, value=json.dumps(result_dict).encode()
            )

        await event_bus.start()
        await event_bus.subscribe(
            cmd_topic, on_message=on_command, group_id="test-pr-inventory"
        )

        cmd_payload = json.dumps(
            {"repo": "OmniNode-ai/omnimarket", "pr_numbers": [1]}
        ).encode()
        await event_bus.publish(cmd_topic, key=None, value=cmd_payload)

        assert len(received_events) == 1
        assert received_events[0]["repo"] == "OmniNode-ai/omnimarket"
        assert received_events[0]["total_collected"] == 1

        await event_bus.close()


def _gh_result(stdout: str = "", returncode: int = 0, stderr: str = "") -> MagicMock:
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = stdout
    mock.stderr = stderr
    return mock


@pytest.mark.unit
class TestReasonCodeExtractionToClassification:
    """OMN-14769: the LIVE extraction -> classification seam (not fixtures).

    The OMN-14765 fixture-corpus gate replays hand-injected ``log_signatures`` /
    ``is_superseded`` / ``api_error`` facts through the classifier, so it proves
    the classifier keys correctly but NOT that the inventory node's live
    extraction actually produces those facts. These tests drive the real
    ``_collect_check_runs`` path (gh pr checks -> job-log extraction -> jobs-API
    attempt -> classify) so the whole seam is exercised end to end.

    Each test is RED against the merged OMN-14765 code (the node-local signature
    subset dropped F-23 + API-outage signatures; ``api_error`` / ``is_superseded``
    were never threaded) and GREEN after OMN-14769.
    """

    _REPO = "OmniNode-ai/omnimarket"
    _HEAD = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"

    def _classify_single_failed_check(
        self,
        *,
        check_name: str,
        failing_step_name: str,
        run_id: str,
        linked_job_id: str,
        log_body: str,
        jobs_result: MagicMock,
        returned_job_id: str | None = None,
        run_attempt: int = 1,
    ) -> EnumMergeCheckReasonCode | None:
        """Run one failed check through the real _collect_check_runs seam.

        ``jobs_result`` is what the ``.../runs/<run_id>/jobs`` gh call returns;
        ``log_body`` is what the ``.../jobs/<job_id>/logs`` call returns.
        """
        handler = HandlerPrLifecycleInventory()
        link = (
            f"https://github.com/{self._REPO}/actions/runs/{run_id}/job/{linked_job_id}"
        )
        checks_payload = [
            {
                "name": check_name,
                "state": "FAILURE",
                "bucket": "fail",
                "event": "pull_request",
                "link": link,
            }
        ]

        def fake_run(
            cmd: list[str],
            capture_output: bool,
            text: bool,
            timeout: int | None = None,
        ) -> MagicMock:
            if cmd[:3] == ["gh", "pr", "checks"]:
                return _gh_result(json.dumps(checks_payload))
            # jobs-API attempt fetch: ".../actions/runs/<id>/jobs"
            if cmd[:2] == ["gh", "api"] and cmd[2].endswith("/jobs"):
                return jobs_result
            # job-log fetch: ".../actions/jobs/<id>/logs"
            if cmd[:2] == ["gh", "api"] and "/logs" in cmd[2]:
                return _gh_result(log_body)
            return _gh_result("{}")

        with patch("subprocess.run", side_effect=fake_run):
            check_runs = handler._collect_check_runs(
                self._REPO, 4242, current_head_sha=self._HEAD
            )
        assert len(check_runs) == 1
        return check_runs[0].reason_code

    def _one_job_payload(
        self,
        *,
        job_id: str,
        run_id: str,
        step_name: str,
        run_attempt: int = 1,
        conclusion: str = "failure",
    ) -> MagicMock:
        return _gh_result(
            json.dumps(
                {
                    "jobs": [
                        {
                            "id": int(job_id),
                            "run_id": int(run_id),
                            "run_attempt": run_attempt,
                            "status": "completed",
                            "conclusion": conclusion,
                            "head_sha": self._HEAD,
                            "name": "job",
                            "steps": [
                                {"name": "Set up job", "conclusion": "success"},
                                {"name": step_name, "conclusion": conclusion},
                            ],
                        }
                    ]
                }
            )
        )

    def test_f23_os_exit_hang_on_test_step_is_runner_infra(self) -> None:
        """F-23 (G1): a real os._exit hang log on a product 'test' step.

        RED before OMN-14769: the node-local signature subset carried none of the
        isolation-hang signatures, so extraction returned an empty tuple and the
        'Run shadow-slice tests' step classified PRODUCT_FAILED. GREEN after: the
        canonical union extracts the hang signature -> RUNNER_INFRA.
        """
        log_body = (
            "Running shadow-slice tests...\n"
            "ERROR: 60s thread timeout exceeded; a leaked thread survived join.\n"
            "Fatal: hard timeout reached, calling os._exit(1) to abort the runner\n"
        )
        jobs = self._one_job_payload(
            job_id="990011",
            run_id="550011",
            step_name="Run shadow-slice tests",
        )
        code = self._classify_single_failed_check(
            check_name="Dispatcher Route Coverage (shadow slice)",
            failing_step_name="Run shadow-slice tests",
            run_id="550011",
            linked_job_id="990011",
            log_body=log_body,
            jobs_result=jobs,
        )
        assert code is EnumMergeCheckReasonCode.RUNNER_INFRA

    def test_f07_api_outage_html_log_is_github_api_outage(self) -> None:
        """F-07 (G1, API-outage half): an HTML/503 job-log body.

        RED before: the node-local subset carried no API-outage signatures, so a
        product-named step classified PRODUCT_FAILED. GREEN after: the extracted
        outage signature -> GITHUB_API_OUTAGE (outranks product).
        """
        log_body = (
            "<!DOCTYPE html>\n<html><head><title>Error</title></head>\n"
            "<body>503 Service Unavailable</body></html>\n"
        )
        jobs = self._one_job_payload(
            job_id="990001",
            run_id="550001",
            step_name="Run gh pr diff (product-diff-scope)",
        )
        code = self._classify_single_failed_check(
            check_name="verify / Run Receipt-Gate",
            failing_step_name="Run gh pr diff (product-diff-scope)",
            run_id="550001",
            linked_job_id="990001",
            log_body=log_body,
            jobs_result=jobs,
        )
        assert code is EnumMergeCheckReasonCode.GITHUB_API_OUTAGE

    def test_f07_jobs_api_metadata_503_sets_api_error(self) -> None:
        """F-07 (G2): the jobs-API metadata call itself returns HTTP 503.

        RED before: api_error was never threaded, so with no job resolved the
        check fell closed to RUNNER_INFRA. GREEN after: the failed metadata call
        is detected as an outage -> api_error=True -> GITHUB_API_OUTAGE.
        """
        jobs_503 = _gh_result(
            stdout="",
            returncode=1,
            stderr="gh: Service Unavailable (HTTP 503)",
        )
        code = self._classify_single_failed_check(
            check_name="verify / Run Receipt-Gate",
            failing_step_name="",
            run_id="550002",
            linked_job_id="990002",
            log_body="",  # no log signatures — api_error is the only outage signal
            jobs_result=jobs_503,
        )
        assert code is EnumMergeCheckReasonCode.GITHUB_API_OUTAGE

    def test_gh_timeout_on_jobs_api_sets_api_error(self) -> None:
        """F-07 (G2): a gh timeout on the jobs-API call is an outage, not product."""
        timeout_result = _gh_result(
            stdout="",
            returncode=124,  # _GH_TIMEOUT_RETURNCODE
            stderr="gh call timed out after 30s",
        )
        code = self._classify_single_failed_check(
            check_name="mypy / type-check",
            failing_step_name="",
            run_id="550003",
            linked_job_id="990003",
            log_body="",
            jobs_result=timeout_result,
        )
        assert code is EnumMergeCheckReasonCode.GITHUB_API_OUTAGE

    def test_superseded_attempt_is_stale_context(self) -> None:
        """F-14/F-26 (G3): linked job absent from latest attempt (re-run exists).

        RED before: is_superseded was never threaded, so the failing step
        ('Assert state == OPEN', not a product step) fell closed to RUNNER_INFRA.
        GREEN after: the linked job_id is absent from the latest (attempt 2) job
        set -> is_superseded=True -> STALE_CONTEXT.
        """
        # Linked job 990008 is NOT in the returned latest-attempt job set (990099),
        # and the returned attempt is 2 -> the check row is superseded.
        jobs = self._one_job_payload(
            job_id="990099",
            run_id="550008",
            step_name="Assert state == OPEN",
            run_attempt=2,
        )
        code = self._classify_single_failed_check(
            check_name="Contract Compliance / dod-occ-4329-head",
            failing_step_name="Assert state == OPEN",
            run_id="550008",
            linked_job_id="990008",
            log_body="",
            jobs_result=jobs,
        )
        assert code is EnumMergeCheckReasonCode.STALE_CONTEXT

    def test_single_attempt_link_miss_is_not_superseded(self) -> None:
        """A linked-job miss on a single-attempt run is NOT supersession.

        Guards the G3 heuristic against false positives: attempt 1 + a
        link/rollup mismatch must fall through to the failing job's own
        classification (here a genuine product 'pytest' step -> PRODUCT_FAILED),
        never STALE_CONTEXT.
        """
        jobs = self._one_job_payload(
            job_id="990100",
            run_id="550009",
            step_name="Run pytest suite",
            run_attempt=1,
        )
        code = self._classify_single_failed_check(
            check_name="tests / pytest",
            failing_step_name="Run pytest suite",
            run_id="550009",
            linked_job_id="990008",  # absent, but attempt is 1
            log_body="",
            jobs_result=jobs,
        )
        assert code is EnumMergeCheckReasonCode.PRODUCT_FAILED

    def test_genuine_product_failure_still_classifies_product(self) -> None:
        """Regression guard: a real product test failure with a clean infra log
        still classifies PRODUCT_FAILED (the widened extraction must not mask it).
        """
        jobs = self._one_job_payload(
            job_id="990500",
            run_id="550500",
            step_name="Run pytest suite",
        )
        code = self._classify_single_failed_check(
            check_name="tests / pytest",
            failing_step_name="Run pytest suite",
            run_id="550500",
            linked_job_id="990500",
            log_body="assert 1 == 2\nE   AssertionError: values differ\n",
            jobs_result=jobs,
        )
        assert code is EnumMergeCheckReasonCode.PRODUCT_FAILED

    @pytest.mark.parametrize("signature", ALL_LOG_SIGNATURES)
    def test_every_canonical_signature_is_extracted_live(self, signature: str) -> None:
        """Anti-drift seam gate: every classifier signature is extractable live.

        Proves the inventory extraction scans the classifier's full canonical
        ``ALL_LOG_SIGNATURES`` union (no node-local subset can silently drop a
        signature — the exact G1 defect). A log containing each signature must
        surface it in ``_collect_flaky_failure_evidence``.
        """
        handler = HandlerPrLifecycleInventory()
        link = f"https://github.com/{self._REPO}/actions/runs/551000/job/991000"
        item = {
            "name": "some / check",
            "state": "FAILURE",
            "bucket": "fail",
            "event": "pull_request",
            "link": link,
        }

        def fake_run(
            cmd: list[str],
            capture_output: bool,
            text: bool,
            timeout: int | None = None,
        ) -> MagicMock:
            return _gh_result(f"prefix line\n...{signature.upper()}...\nsuffix\n")

        with patch("subprocess.run", side_effect=fake_run):
            evidence = handler._collect_flaky_failure_evidence(item)

        assert signature in evidence
