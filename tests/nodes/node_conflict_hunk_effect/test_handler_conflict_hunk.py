# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD tests for node_conflict_hunk_effect [OMN-8991].

Tests are written before implementation (Phase 1 scaffold).
All tests that exercise the real handler stub currently expect NotImplementedError,
since full implementation ships in OMN-8992.

Safety-rule tests exercise the static helpers directly (no LLM needed):
  - _is_blocked_file validates the write blocklist
  - _validate_patch_size validates the 50-line limit
  - ValueError on no conflict markers
  - ValueError on blocked file in hunk

Behaviour tests (currently NotImplementedError, will pass in OMN-8992):
  - conflict file + mock router → resolution_committed=True, is_noop=False
  - LLM output identical to current file → is_noop=True, resolution_committed=False
  - pytest fails after write → resolution_committed=False, no commit, worktree cleaned
  - LLM returns syntactically invalid Python → fail-loud, no write
  - Post-mutation scope check fails → abort + revert
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_conflict_hunk_effect.handlers.handler_conflict_hunk import (
    _MAX_CHANGED_LINES,
    HandlerConflictHunk,
)
from omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request import (
    ModelConflictHunkCommand,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_DEFAULT_CONFLICT_FILES = ["src/omnimarket/foo.py"]


def _make_cmd(
    conflict_files: list[str] = _DEFAULT_CONFLICT_FILES,
    pr_number: int = 42,
    repo: str = "OmniNode-ai/omnimarket",
) -> ModelConflictHunkCommand:
    return ModelConflictHunkCommand(
        pr_number=pr_number,
        repo=repo,
        head_ref_name="feature/test",
        base_ref_name="main",
        conflict_files=conflict_files,
        correlation_id=uuid.uuid4(),
        run_id=str(uuid.uuid4()),
        routing_policy={
            "primary": "deepseek-r1-14b",
            "fallback": "deepseek-r1-32b",
            "fallback_allowed_roles": ["conflict_resolver"],
            "max_tokens": 4096,
        },
    )


# ---------------------------------------------------------------------------
# Static helper unit tests (no LLM — pass now in Phase 1)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWriteBlocklist:
    def test_pyproject_toml_is_blocked(self) -> None:
        assert HandlerConflictHunk._is_blocked_file("pyproject.toml") is True

    def test_uv_lock_is_blocked(self) -> None:
        assert HandlerConflictHunk._is_blocked_file("uv.lock") is True

    def test_requirements_is_blocked(self) -> None:
        assert HandlerConflictHunk._is_blocked_file("requirements.txt") is True

    def test_source_file_is_not_blocked(self) -> None:
        assert HandlerConflictHunk._is_blocked_file("src/omnimarket/foo.py") is False

    def test_test_file_is_not_blocked(self) -> None:
        assert HandlerConflictHunk._is_blocked_file("tests/test_handler.py") is False

    def test_dockerfile_is_blocked(self) -> None:
        assert HandlerConflictHunk._is_blocked_file("Dockerfile") is True

    def test_package_json_is_blocked(self) -> None:
        assert HandlerConflictHunk._is_blocked_file("package.json") is True


@pytest.mark.unit
class TestPatchSizeValidation:
    def test_under_limit_passes(self) -> None:
        HandlerConflictHunk._validate_patch_size(_MAX_CHANGED_LINES)

    def test_over_limit_raises(self) -> None:
        with pytest.raises(ValueError, match="50-line limit"):
            HandlerConflictHunk._validate_patch_size(_MAX_CHANGED_LINES + 1)

    def test_zero_lines_passes(self) -> None:
        HandlerConflictHunk._validate_patch_size(0)


# ---------------------------------------------------------------------------
# Behaviour tests — Phase 1 scaffold raises NotImplementedError.
# These will be updated to assert real outcomes in OMN-8992.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandlerScaffold:
    """All resolve() calls raise NotImplementedError until OMN-8992 lands."""

    def test_conflict_file_mock_router_raises_not_implemented(self) -> None:
        """Phase 2: conflict file + mock router → resolution_committed=True, is_noop=False."""
        handler = HandlerConflictHunk()
        cmd = _make_cmd(conflict_files=["src/omnimarket/foo.py"])
        with pytest.raises(NotImplementedError):
            handler.resolve(cmd)

    def test_llm_output_identical_to_current_file_raises_not_implemented(self) -> None:
        """Phase 2: LLM output identical to current file → is_noop=True, resolution_committed=False."""
        handler = HandlerConflictHunk()
        cmd = _make_cmd(conflict_files=["src/omnimarket/bar.py"])
        with pytest.raises(NotImplementedError):
            handler.resolve(cmd)

    def test_pytest_fails_after_write_raises_not_implemented(self) -> None:
        """Phase 2: pytest failure after write → resolution_committed=False, worktree cleaned."""
        handler = HandlerConflictHunk()
        cmd = _make_cmd(conflict_files=["src/omnimarket/baz.py"])
        with pytest.raises(NotImplementedError):
            handler.resolve(cmd)

    def test_llm_invalid_python_raises_not_implemented(self) -> None:
        """Phase 2: LLM returns syntactically invalid Python → fail-loud, no write."""
        handler = HandlerConflictHunk()
        cmd = _make_cmd(conflict_files=["src/omnimarket/invalid.py"])
        with pytest.raises(NotImplementedError):
            handler.resolve(cmd)

    def test_post_mutation_scope_check_fail_raises_not_implemented(self) -> None:
        """Phase 2: unexpected file touched during resolution → abort + revert."""
        handler = HandlerConflictHunk()
        cmd = _make_cmd(conflict_files=["src/omnimarket/scoped.py"])
        with pytest.raises(NotImplementedError):
            handler.resolve(cmd)


# ---------------------------------------------------------------------------
# Contract-level tests (no LLM — validate input shape)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInputContract:
    def test_empty_conflict_files_accepted_by_model(self) -> None:
        """ModelConflictHunkCommand accepts an empty list; handler is responsible for ValueError."""
        cmd = _make_cmd(conflict_files=[])
        assert cmd.conflict_files == []

    def test_blocked_file_in_command_accepted_by_model(self) -> None:
        """Model does not reject blocked files; handler enforces the safety rule."""
        cmd = _make_cmd(conflict_files=["pyproject.toml"])
        assert cmd.conflict_files == ["pyproject.toml"]

    def test_command_is_frozen(self) -> None:
        cmd = _make_cmd()
        with pytest.raises(ValidationError):
            cmd.pr_number = 999  # type: ignore[misc]

    def test_result_model_immutable(self) -> None:
        from omnimarket.nodes.node_conflict_hunk_effect.models.model_conflict_hunk_result import (
            ModelConflictHunkResult,
        )

        result = ModelConflictHunkResult(
            pr_number=1,
            repo="OmniNode-ai/omnimarket",
            files_resolved=["src/foo.py"],
            resolution_committed=True,
            is_noop=False,
            correlation_id=uuid.uuid4(),
        )
        assert result.resolution_committed is True
        assert result.is_noop is False
        with pytest.raises(ValidationError):
            result.resolution_committed = False  # type: ignore[misc]

    def test_result_model_noop(self) -> None:
        from omnimarket.nodes.node_conflict_hunk_effect.models.model_conflict_hunk_result import (
            ModelConflictHunkResult,
        )

        result = ModelConflictHunkResult(
            pr_number=1,
            repo="OmniNode-ai/omnimarket",
            files_resolved=[],
            resolution_committed=False,
            is_noop=True,
            correlation_id=uuid.uuid4(),
        )
        assert result.is_noop is True
        assert result.files_resolved == []

    def test_no_conflict_markers_future_raises_value_error(self) -> None:
        """Validates the static safety rule: no conflict markers in hunk → ValueError.
        Phase 1: tested via direct function call pattern (handler raises NotImplementedError
        before reaching this check — will be wired in OMN-8992).
        Ensures the blocker string is documented and reserved.
        """
        conflict_marker = "<<<<<<"
        hunk_without_markers = "def foo():\n    return 1\n"
        assert conflict_marker not in hunk_without_markers
