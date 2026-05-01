# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_pr_lifecycle_orchestrator Kafka consumer wiring (OMN-10465)."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_pr_lifecycle_orchestrator.consumer import (
    TOPIC_PR_LIFECYCLE_COMPLETED,
    TOPIC_PR_LIFECYCLE_FAILED,
    TOPIC_PR_LIFECYCLE_START,
    _build_failure_payload,
    _parse_command,
)


@pytest.mark.unit
class TestPrLifecycleConsumerTopics:
    """Topic constants match contract.yaml declarations."""

    def test_start_topic(self) -> None:
        assert (
            TOPIC_PR_LIFECYCLE_START
            == "onex.cmd.omnimarket.pr-lifecycle-orchestrator-start.v1"
        )

    def test_completed_topic(self) -> None:
        assert (
            TOPIC_PR_LIFECYCLE_COMPLETED
            == "onex.evt.omnimarket.pr-lifecycle-orchestrator-completed.v1"
        )

    def test_failed_topic(self) -> None:
        assert (
            TOPIC_PR_LIFECYCLE_FAILED
            == "onex.evt.omnimarket.pr-lifecycle-orchestrator-failed.v1"
        )


@pytest.mark.unit
class TestPrLifecycleParseCommand:
    """_parse_command extracts and defaults fields from raw Kafka payloads."""

    def test_defaults_when_empty_payload(self) -> None:
        cmd = _parse_command({})
        assert cmd["dry_run"] is False
        assert cmd["inventory_only"] is False
        assert cmd["fix_only"] is False
        assert cmd["merge_only"] is False
        assert cmd["repos"] == ""
        assert cmd["max_parallel_polish"] == 20
        assert cmd["enable_auto_rebase"] is True
        assert cmd["use_dag_ordering"] is True
        assert isinstance(cmd["correlation_id"], str)
        assert len(cmd["correlation_id"]) > 0

    def test_run_id_derived_from_correlation_id_when_missing(self) -> None:
        cmd = _parse_command({"correlation_id": "abc123def"})
        # run_id should contain the first 6 chars of correlation_id
        assert "abc123" in cmd["run_id"]

    def test_explicit_run_id_accepted(self) -> None:
        cmd = _parse_command({"run_id": "my-run-2026"})
        assert cmd["run_id"] == "my-run-2026"

    def test_run_id_sanitized_for_path_safety(self) -> None:
        # run_id must match [A-Za-z0-9._-] — special chars get replaced
        cmd = _parse_command({"run_id": "run/with/slashes"})
        import re

        assert re.match(r"^[A-Za-z0-9._-]+$", cmd["run_id"])

    def test_run_id_too_long_truncated(self) -> None:
        cmd = _parse_command({"run_id": "x" * 200})
        assert len(cmd["run_id"]) <= 128

    def test_explicit_values_override_defaults(self) -> None:
        cmd = _parse_command(
            {
                "correlation_id": "corr-999",
                "dry_run": True,
                "inventory_only": True,
                "repos": "OmniNode-ai/omniclaude",
                "max_parallel_polish": 5,
                "enable_auto_rebase": False,
                "use_dag_ordering": False,
            }
        )
        assert cmd["correlation_id"] == "corr-999"
        assert cmd["dry_run"] is True
        assert cmd["inventory_only"] is True
        assert cmd["repos"] == "OmniNode-ai/omniclaude"
        assert cmd["max_parallel_polish"] == 5
        assert cmd["enable_auto_rebase"] is False
        assert cmd["use_dag_ordering"] is False

    def test_correlation_id_generated_when_missing(self) -> None:
        cmd1 = _parse_command({})
        cmd2 = _parse_command({})
        assert cmd1["correlation_id"] != cmd2["correlation_id"]

    def test_failure_event_has_required_fields(self) -> None:
        failure = _build_failure_payload(
            {"correlation_id": "test-corr", "run_id": "run-abc"},
            RuntimeError("handler raised"),
        )
        assert failure["correlation_id"] == "test-corr"
        assert "error" in failure
        assert "run_id" in failure
