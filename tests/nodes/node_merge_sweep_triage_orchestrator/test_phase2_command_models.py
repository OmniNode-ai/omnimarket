# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for Phase 2 command models [OMN-8987].

TDD: tests written before implementation.
Covers: ModelThreadReplyCommand, ModelConflictHunkCommand, ModelCiFixCommand.
"""

from __future__ import annotations

import json
import uuid

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request import (
    ModelCiFixCommand,
    ModelConflictHunkCommand,
    ModelThreadReplyCommand,
)

CORR_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
RUN_ID = "run-abc-123"


# ---------------------------------------------------------------------------
# ModelThreadReplyCommand
# ---------------------------------------------------------------------------


class TestModelThreadReplyCommand:
    def _make(self, **overrides: object) -> ModelThreadReplyCommand:
        defaults: dict[str, object] = {
            "pr_number": 42,
            "repo": "OmniNode-ai/omnimarket",
            "thread_comment_ids": ["comment-1", "comment-2"],
            "correlation_id": CORR_ID,
            "run_id": RUN_ID,
            "routing_policy": {"model": "qwen3-coder", "temperature": 0.0},
        }
        return ModelThreadReplyCommand(**{**defaults, **overrides})

    def test_instantiation(self) -> None:
        cmd = self._make()
        assert cmd.pr_number == 42
        assert cmd.repo == "OmniNode-ai/omnimarket"
        assert cmd.thread_comment_ids == ["comment-1", "comment-2"]
        assert cmd.correlation_id == CORR_ID
        assert cmd.run_id == RUN_ID
        assert cmd.routing_policy == {"model": "qwen3-coder", "temperature": 0.0}

    def test_routing_policy_non_none(self) -> None:
        cmd = self._make(routing_policy={"model": "any"})
        assert cmd.routing_policy is not None

    def test_frozen(self) -> None:
        cmd = self._make()
        with pytest.raises(ValidationError):
            cmd.pr_number = 99  # type: ignore[misc]

    def test_extra_field_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ModelThreadReplyCommand(
                pr_number=1,
                repo="r/r",
                thread_comment_ids=[],
                correlation_id=CORR_ID,
                run_id=RUN_ID,
                routing_policy={},
                unknown_field="boom",
            )

    def test_serialization_round_trip(self) -> None:
        cmd = self._make()
        data = cmd.model_dump()
        restored = ModelThreadReplyCommand.model_validate(data)
        assert restored == cmd

    def test_json_round_trip(self) -> None:
        cmd = self._make()
        raw = cmd.model_dump_json()
        parsed = json.loads(raw)
        restored = ModelThreadReplyCommand.model_validate(parsed)
        assert restored == cmd

    def test_thread_comment_ids_empty_list(self) -> None:
        cmd = self._make(thread_comment_ids=[])
        assert cmd.thread_comment_ids == []

    def test_routing_policy_empty_dict(self) -> None:
        cmd = self._make(routing_policy={})
        assert cmd.routing_policy == {}

    def test_routing_policy_complex_nested(self) -> None:
        policy: dict[str, object] = {
            "model": "deepseek",
            "fallback": ["gpt-4o", "claude-3"],
            "options": {"temperature": 0.2, "max_tokens": 4096},
        }
        cmd = self._make(routing_policy=policy)
        assert cmd.routing_policy["fallback"] == ["gpt-4o", "claude-3"]  # type: ignore[index]

    @pytest.mark.unit
    def test_marked_unit(self) -> None:
        assert self._make() is not None


# ---------------------------------------------------------------------------
# ModelConflictHunkCommand
# ---------------------------------------------------------------------------


class TestModelConflictHunkCommand:
    def _make(self, **overrides: object) -> ModelConflictHunkCommand:
        defaults: dict[str, object] = {
            "pr_number": 77,
            "repo": "OmniNode-ai/omnibase_core",
            "head_ref_name": "jonahgabriel/feature-branch",
            "base_ref_name": "main",
            "conflict_files": ["src/foo.py", "src/bar.py"],
            "correlation_id": CORR_ID,
            "run_id": RUN_ID,
            "routing_policy": {"model": "qwen3-coder"},
        }
        return ModelConflictHunkCommand(**{**defaults, **overrides})

    def test_instantiation(self) -> None:
        cmd = self._make()
        assert cmd.pr_number == 77
        assert cmd.repo == "OmniNode-ai/omnibase_core"
        assert cmd.head_ref_name == "jonahgabriel/feature-branch"
        assert cmd.base_ref_name == "main"
        assert cmd.conflict_files == ["src/foo.py", "src/bar.py"]
        assert cmd.correlation_id == CORR_ID
        assert cmd.run_id == RUN_ID
        assert cmd.routing_policy == {"model": "qwen3-coder"}

    def test_routing_policy_non_none(self) -> None:
        cmd = self._make(routing_policy={"model": "any"})
        assert cmd.routing_policy is not None

    def test_frozen(self) -> None:
        cmd = self._make()
        with pytest.raises(ValidationError):
            cmd.pr_number = 0  # type: ignore[misc]

    def test_extra_field_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ModelConflictHunkCommand(
                pr_number=1,
                repo="r/r",
                head_ref_name="h",
                base_ref_name="main",
                conflict_files=[],
                correlation_id=CORR_ID,
                run_id=RUN_ID,
                routing_policy={},
                extra="bad",
            )

    def test_serialization_round_trip(self) -> None:
        cmd = self._make()
        data = cmd.model_dump()
        restored = ModelConflictHunkCommand.model_validate(data)
        assert restored == cmd

    def test_json_round_trip(self) -> None:
        cmd = self._make()
        raw = cmd.model_dump_json()
        parsed = json.loads(raw)
        restored = ModelConflictHunkCommand.model_validate(parsed)
        assert restored == cmd

    def test_single_conflict_file(self) -> None:
        cmd = self._make(conflict_files=["only_one.py"])
        assert len(cmd.conflict_files) == 1

    def test_empty_conflict_files(self) -> None:
        cmd = self._make(conflict_files=[])
        assert cmd.conflict_files == []

    @pytest.mark.unit
    def test_marked_unit(self) -> None:
        assert self._make() is not None


# ---------------------------------------------------------------------------
# ModelCiFixCommand
# ---------------------------------------------------------------------------


class TestModelCiFixCommand:
    def _make(self, **overrides: object) -> ModelCiFixCommand:
        defaults: dict[str, object] = {
            "pr_number": 333,
            "repo": "OmniNode-ai/omnimarket",
            "run_id_github": "12345678",
            "failing_job_name": "test (3.12)",
            "correlation_id": CORR_ID,
            "run_id": RUN_ID,
            "routing_policy": {"model": "deepseek-r1"},
        }
        return ModelCiFixCommand(**{**defaults, **overrides})

    def test_instantiation(self) -> None:
        cmd = self._make()
        assert cmd.pr_number == 333
        assert cmd.repo == "OmniNode-ai/omnimarket"
        assert cmd.run_id_github == "12345678"
        assert cmd.failing_job_name == "test (3.12)"
        assert cmd.correlation_id == CORR_ID
        assert cmd.run_id == RUN_ID
        assert cmd.routing_policy == {"model": "deepseek-r1"}

    def test_routing_policy_non_none(self) -> None:
        cmd = self._make(routing_policy={"model": "any"})
        assert cmd.routing_policy is not None

    def test_frozen(self) -> None:
        cmd = self._make()
        with pytest.raises(ValidationError):
            cmd.pr_number = 0  # type: ignore[misc]

    def test_extra_field_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ModelCiFixCommand(
                pr_number=1,
                repo="r/r",
                run_id_github="gh-run",
                failing_job_name="job",
                correlation_id=CORR_ID,
                run_id=RUN_ID,
                routing_policy={},
                unexpected="field",
            )

    def test_serialization_round_trip(self) -> None:
        cmd = self._make()
        data = cmd.model_dump()
        restored = ModelCiFixCommand.model_validate(data)
        assert restored == cmd

    def test_json_round_trip(self) -> None:
        cmd = self._make()
        raw = cmd.model_dump_json()
        parsed = json.loads(raw)
        restored = ModelCiFixCommand.model_validate(parsed)
        assert restored == cmd

    def test_routing_policy_with_fallback_list(self) -> None:
        policy: dict[str, object] = {"model": "primary", "fallback": ["secondary"]}
        cmd = self._make(routing_policy=policy)
        assert cmd.routing_policy["fallback"] == ["secondary"]  # type: ignore[index]

    @pytest.mark.unit
    def test_marked_unit(self) -> None:
        assert self._make() is not None
