# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain / unit tests for node_repo_health_classify_compute.

Verifies deterministic failure-origin classification using pure input envelopes.
Zero network calls — all state is injected via ModelRepoHealthFailureEnvelope.

Related:
    - OMN-13583: node_repo_health_classify_compute (keystone of the lane)
    - OMN-13316: Epic — merge-sweep & evidence-automation hardening
    - OMN-13027: dev-baseline ratchet (source of dev_baseline_paths)
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.events.repo_health import (
    EnumFailureOrigin,
    ModelRepoHealthClassification,
    ModelRepoHealthFailureEnvelope,
)
from omnimarket.nodes.node_repo_health_classify_compute.handlers.handler_repo_health_classify import (
    HandlerRepoHealthClassify,
)


@pytest.mark.unit
class TestRepoHealthClassifyComputeGoldenChain:
    """Golden chain: failure envelope in -> origin classification out."""

    async def test_pr_scoped_when_failing_path_in_changed_set(self) -> None:
        """A failing path inside the PR's changed-file set -> PR_SCOPED."""
        handler = HandlerRepoHealthClassify()
        correlation_id = uuid4()
        envelope = ModelRepoHealthFailureEnvelope(
            correlation_id=correlation_id,
            repo="OmniNode-ai/omnimarket",
            pr_number=1234,
            branch="jonah/omn-13583",
            failing_command="uv run pytest tests/",
            exit_code=1,
            failing_paths=("src/omnimarket/nodes/node_x/handler.py",),
            pr_changed_paths=(
                "src/omnimarket/nodes/node_x/handler.py",
                "docs/readme.md",
            ),
            dev_baseline_paths=(),
            external_markers=(),
        )

        result = await handler.handle(envelope)

        assert result.origin == EnumFailureOrigin.PR_SCOPED
        assert result.matched_paths == ("src/omnimarket/nodes/node_x/handler.py",)
        assert result.correlation_id == correlation_id
        assert result.repo == "OmniNode-ai/omnimarket"
        assert result.pr_number == 1234
        assert result.failing_command == "uv run pytest tests/"
        assert "src/omnimarket/nodes/node_x/handler.py" in result.reason

    async def test_repo_baseline_when_failing_path_only_on_baseline(self) -> None:
        """Failing path not in PR set but known-failing on the dev baseline -> REPO_BASELINE."""
        handler = HandlerRepoHealthClassify()
        envelope = ModelRepoHealthFailureEnvelope(
            correlation_id=uuid4(),
            repo="OmniNode-ai/omnimarket",
            pr_number=4321,
            branch="jonah/omn-13583",
            failing_command="uv run pre-commit run --all-files",
            exit_code=1,
            failing_paths=("src/omnimarket/legacy/old_module.py",),
            pr_changed_paths=("src/omnimarket/nodes/node_y/handler.py",),
            dev_baseline_paths=("src/omnimarket/legacy/old_module.py",),
            external_markers=(),
        )

        result = await handler.handle(envelope)

        assert result.origin == EnumFailureOrigin.REPO_BASELINE
        assert result.matched_paths == ("src/omnimarket/legacy/old_module.py",)
        assert "baseline" in result.reason.lower()

    async def test_external_dependency_when_markers_and_no_paths(self) -> None:
        """No failing paths but external markers present -> EXTERNAL_DEPENDENCY."""
        handler = HandlerRepoHealthClassify()
        envelope = ModelRepoHealthFailureEnvelope(
            correlation_id=uuid4(),
            repo="OmniNode-ai/omnimarket",
            pr_number=None,
            branch="dev",
            failing_command="uv run pytest tests/integration",
            exit_code=2,
            failing_paths=(),
            pr_changed_paths=(),
            dev_baseline_paths=(),
            external_markers=("connection refused", "EHOSTUNREACH"),
        )

        result = await handler.handle(envelope)

        assert result.origin == EnumFailureOrigin.EXTERNAL_DEPENDENCY
        assert result.matched_paths == ()
        assert result.pr_number is None
        assert "connection refused" in result.reason or "EHOSTUNREACH" in result.reason

    async def test_external_dependency_when_markers_with_paths(self) -> None:
        """Failing paths present (not PR, not baseline) but external markers -> EXTERNAL_DEPENDENCY."""
        handler = HandlerRepoHealthClassify()
        envelope = ModelRepoHealthFailureEnvelope(
            correlation_id=uuid4(),
            repo="OmniNode-ai/omnimarket",
            pr_number=77,
            branch="jonah/omn-13583",
            failing_command="uv run pytest tests/integration/test_kafka.py",
            exit_code=1,
            failing_paths=("tests/integration/test_kafka.py",),
            pr_changed_paths=("src/omnimarket/nodes/node_z/handler.py",),
            dev_baseline_paths=(),
            external_markers=("missing secret",),
        )

        result = await handler.handle(envelope)

        assert result.origin == EnumFailureOrigin.EXTERNAL_DEPENDENCY
        assert "missing secret" in result.reason

    async def test_unknown_when_path_neither_pr_nor_baseline_no_markers(self) -> None:
        """Failing path not in PR set, not on baseline, no markers -> UNKNOWN (conservative)."""
        handler = HandlerRepoHealthClassify()
        envelope = ModelRepoHealthFailureEnvelope(
            correlation_id=uuid4(),
            repo="OmniNode-ai/omnimarket",
            pr_number=909,
            branch="jonah/omn-13583",
            failing_command="uv run pytest tests/",
            exit_code=1,
            failing_paths=("src/omnimarket/nodes/node_q/handler.py",),
            pr_changed_paths=("src/omnimarket/nodes/node_r/handler.py",),
            dev_baseline_paths=("src/omnimarket/nodes/node_s/handler.py",),
            external_markers=(),
        )

        result = await handler.handle(envelope)

        assert result.origin == EnumFailureOrigin.UNKNOWN
        # Never silently REPO_BASELINE on ambiguity.
        assert result.origin != EnumFailureOrigin.REPO_BASELINE

    async def test_unknown_when_no_paths_and_no_markers(self) -> None:
        """No failing paths, no external markers -> UNKNOWN (cannot prove anything)."""
        handler = HandlerRepoHealthClassify()
        envelope = ModelRepoHealthFailureEnvelope(
            correlation_id=uuid4(),
            repo="OmniNode-ai/omnimarket",
            pr_number=11,
            branch="jonah/omn-13583",
            failing_command="uv run mypy src/",
            exit_code=1,
            failing_paths=(),
            pr_changed_paths=("src/omnimarket/nodes/node_t/handler.py",),
            dev_baseline_paths=(),
            external_markers=(),
        )

        result = await handler.handle(envelope)

        assert result.origin == EnumFailureOrigin.UNKNOWN

    async def test_ambiguous_partial_baseline_stays_unknown(self) -> None:
        """Some failing paths on baseline but at least one is not -> UNKNOWN, never REPO_BASELINE.

        Conservative rule: REPO_BASELINE requires ALL failing paths (none in the PR
        set) to be on the dev baseline. A partial baseline overlap is ambiguous.
        """
        handler = HandlerRepoHealthClassify()
        envelope = ModelRepoHealthFailureEnvelope(
            correlation_id=uuid4(),
            repo="OmniNode-ai/omnimarket",
            pr_number=222,
            branch="jonah/omn-13583",
            failing_command="uv run pre-commit run --all-files",
            exit_code=1,
            failing_paths=(
                "src/omnimarket/legacy/a.py",
                "src/omnimarket/legacy/b.py",
            ),
            pr_changed_paths=("src/omnimarket/nodes/node_u/handler.py",),
            dev_baseline_paths=("src/omnimarket/legacy/a.py",),  # only a.py, not b.py
            external_markers=(),
        )

        result = await handler.handle(envelope)

        assert result.origin == EnumFailureOrigin.UNKNOWN
        assert result.origin != EnumFailureOrigin.REPO_BASELINE

    async def test_pr_scoped_takes_precedence_over_baseline_and_markers(self) -> None:
        """If any failing path is in the PR set, PR_SCOPED wins over baseline/markers."""
        handler = HandlerRepoHealthClassify()
        shared = "src/omnimarket/nodes/node_v/handler.py"
        envelope = ModelRepoHealthFailureEnvelope(
            correlation_id=uuid4(),
            repo="OmniNode-ai/omnimarket",
            pr_number=333,
            branch="jonah/omn-13583",
            failing_command="uv run pytest tests/",
            exit_code=1,
            failing_paths=(shared,),
            pr_changed_paths=(shared,),
            dev_baseline_paths=(shared,),  # also on baseline, but PR set wins
            external_markers=("connection refused",),  # also markers, but PR set wins
        )

        result = await handler.handle(envelope)

        assert result.origin == EnumFailureOrigin.PR_SCOPED
        assert result.matched_paths == (shared,)

    async def test_repair_lane_baseline_can_still_arm_pr_when_pr_scoped_clean(
        self,
    ) -> None:
        """REPO_BASELINE origin does not depend on PR contents being clean — it is path-driven."""
        handler = HandlerRepoHealthClassify()
        envelope = ModelRepoHealthFailureEnvelope(
            correlation_id=uuid4(),
            repo="OmniNode-ai/omnimarket",
            pr_number=444,
            branch="jonah/omn-13583",
            failing_command="uv run pre-commit run --all-files",
            exit_code=1,
            failing_paths=("docs/legacy/stale.md",),
            pr_changed_paths=(),  # PR changed nothing implicated
            dev_baseline_paths=("docs/legacy/stale.md",),
            external_markers=(),
        )

        result = await handler.handle(envelope)

        assert result.origin == EnumFailureOrigin.REPO_BASELINE

    async def test_determinism_same_input_twice_equal_output(self) -> None:
        """Same input envelope classified twice -> identical output (pure function)."""
        handler = HandlerRepoHealthClassify()
        correlation_id = uuid4()
        envelope = ModelRepoHealthFailureEnvelope(
            correlation_id=correlation_id,
            repo="OmniNode-ai/omnimarket",
            pr_number=555,
            branch="jonah/omn-13583",
            failing_command="uv run pytest tests/",
            exit_code=1,
            failing_paths=("src/omnimarket/nodes/node_w/handler.py",),
            pr_changed_paths=("src/omnimarket/nodes/node_w/handler.py",),
            dev_baseline_paths=(),
            external_markers=(),
        )

        first = await handler.handle(envelope)
        second = await handler.handle(envelope)

        assert isinstance(first, ModelRepoHealthClassification)
        assert first == second
        assert first.model_dump() == second.model_dump()

    async def test_output_carries_no_clock_fields(self) -> None:
        """Classification output has no timestamp/wall-clock fields (determinism guard)."""
        handler = HandlerRepoHealthClassify()
        envelope = ModelRepoHealthFailureEnvelope(
            correlation_id=uuid4(),
            repo="OmniNode-ai/omnimarket",
            pr_number=1,
            branch="dev",
            failing_command="uv run pytest tests/",
            exit_code=1,
            failing_paths=(),
            pr_changed_paths=(),
            dev_baseline_paths=(),
            external_markers=("EHOSTUNREACH",),
        )

        result = await handler.handle(envelope)

        field_names = set(type(result).model_fields)
        clocky = {f for f in field_names if "time" in f or "ts" in f or "date" in f}
        assert clocky == set()
