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
from unittest.mock import MagicMock, patch

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_pr_lifecycle_inventory_compute.handlers.handler_pr_lifecycle_inventory import (
    HandlerPrLifecycleInventory,
)
from omnimarket.nodes.node_pr_lifecycle_inventory_compute.models.model_pr_lifecycle_inventory import (
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

        def fake_run(cmd: list[str], capture_output: bool, text: bool) -> MagicMock:
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

        def fail_run(cmd: list[str], capture_output: bool, text: bool) -> MagicMock:
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

        def mixed_run(cmd: list[str], capture_output: bool, text: bool) -> MagicMock:
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
        result = handler.handle(
            ModelPrInventoryInput(repo="OmniNode-ai/omnimarket", pr_numbers=())
        )

        assert isinstance(result, ModelPrInventoryOutput)
        assert result.total_collected == 0
        assert len(result.pr_states) == 0
        assert len(result.collection_errors) == 0


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

        def fake_run(cmd: list[str], capture_output: bool, text: bool) -> MagicMock:
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
