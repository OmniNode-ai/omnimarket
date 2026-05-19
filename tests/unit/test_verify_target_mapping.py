# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for verify_target_mapping and ModelPrLifecycleStartCommand verify fields."""

from __future__ import annotations

import subprocess

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_pr_lifecycle_orchestrator.verify_target_mapping import (
    EnumVerificationOutcome,
    EnumVerificationTarget,
    _run_command,
    classify_verification_outcome,
    map_changed_files_to_target,
    probe_runtime_health,
    requires_runtime_health,
)


@pytest.mark.unit
class TestMapChangedFilesToTarget:
    def test_projection_file(self) -> None:
        assert (
            map_changed_files_to_target(["src/foo/projection_bar.py"])
            == EnumVerificationTarget.PROJECTION_ROW_CHECK
        )

    def test_projector_file(self) -> None:
        assert (
            map_changed_files_to_target(["src/foo/projector_baz.py"])
            == EnumVerificationTarget.PROJECTION_ROW_CHECK
        )

    def test_handler_file(self) -> None:
        assert (
            map_changed_files_to_target(["src/foo/handler_qux.py"])
            == EnumVerificationTarget.PROJECTION_SINK_CHECK
        )

    def test_route_file(self) -> None:
        assert (
            map_changed_files_to_target(["src/foo/route_x.py"])
            == EnumVerificationTarget.API_ROUTE_CHECK
        )

    def test_api_file(self) -> None:
        assert (
            map_changed_files_to_target(["src/foo/api_y.py"])
            == EnumVerificationTarget.API_ROUTE_CHECK
        )

    def test_pages_api(self) -> None:
        assert (
            map_changed_files_to_target(["pages/api/something.ts"])
            == EnumVerificationTarget.API_ROUTE_CHECK
        )

    def test_drizzle(self) -> None:
        assert (
            map_changed_files_to_target(["drizzle/001.sql"])
            == EnumVerificationTarget.DB_MIGRATION_CHECK
        )

    def test_migrations(self) -> None:
        assert (
            map_changed_files_to_target(["migrations/002.sql"])
            == EnumVerificationTarget.DB_MIGRATION_CHECK
        )

    def test_topics_yaml(self) -> None:
        assert (
            map_changed_files_to_target(["topics.yaml"])
            == EnumVerificationTarget.KAFKA_TOPIC_CHECK
        )

    def test_contract_yaml(self) -> None:
        assert (
            map_changed_files_to_target(["contract.yaml"])
            == EnumVerificationTarget.KAFKA_TOPIC_CHECK
        )

    def test_nested_topics_yaml(self) -> None:
        assert (
            map_changed_files_to_target(["src/omnimarket/nodes/node_foo/topics.yaml"])
            == EnumVerificationTarget.KAFKA_TOPIC_CHECK
        )

    def test_nested_contract_yaml(self) -> None:
        assert (
            map_changed_files_to_target(["src/omnimarket/nodes/node_foo/contract.yaml"])
            == EnumVerificationTarget.KAFKA_TOPIC_CHECK
        )

    def test_no_match(self) -> None:
        assert (
            map_changed_files_to_target(["README.md"])
            == EnumVerificationTarget.SKIPPED_NO_MAPPING
        )

    def test_runtime_auto_wiring_requires_runtime_health(self) -> None:
        changed = ["src/omnibase_infra/runtime/auto_wiring/handler_wiring.py"]

        assert requires_runtime_health(changed) is True
        assert (
            map_changed_files_to_target(changed)
            == EnumVerificationTarget.RUNTIME_HEALTH
        )

    def test_runtime_service_kernel_requires_runtime_health(self) -> None:
        changed = ["src/omnibase_infra/runtime/service_kernel.py"]

        assert requires_runtime_health(changed) is True
        assert (
            map_changed_files_to_target(changed)
            == EnumVerificationTarget.RUNTIME_HEALTH
        )

    def test_handler_underscore_requires_runtime_health(self) -> None:
        changed = ["src/omnibase_infra/nodes/node_foo/handlers/handler_foo.py"]

        assert requires_runtime_health(changed) is True
        assert (
            map_changed_files_to_target(changed)
            == EnumVerificationTarget.RUNTIME_HEALTH
        )

    def test_runtime_health_takes_priority_over_other_targets(self) -> None:
        changed = ["drizzle/001.sql", "src/foo/handlers/handler_bar.py"]

        assert (
            map_changed_files_to_target(changed)
            == EnumVerificationTarget.RUNTIME_HEALTH
        )

    def test_first_match_wins(self) -> None:
        assert (
            map_changed_files_to_target(["src/handler.py", "drizzle/001.sql"])
            == EnumVerificationTarget.PROJECTION_SINK_CHECK
        )

    def test_empty_list(self) -> None:
        assert (
            map_changed_files_to_target([]) == EnumVerificationTarget.SKIPPED_NO_MAPPING
        )


@pytest.mark.unit
class TestClassifyVerificationOutcome:
    def test_exit_zero_is_merged(self) -> None:
        assert (
            classify_verification_outcome(
                EnumVerificationTarget.PROJECTION_ROW_CHECK, 0, "", 1.0, 30
            )
            == EnumVerificationOutcome.MERGED
        )

    def test_exit_nonzero_is_verification_failed(self) -> None:
        assert (
            classify_verification_outcome(
                EnumVerificationTarget.PROJECTION_ROW_CHECK, 1, "error", 1.0, 30
            )
            == EnumVerificationOutcome.VERIFICATION_FAILED
        )

    def test_timeout_exceeded(self) -> None:
        assert (
            classify_verification_outcome(
                EnumVerificationTarget.API_ROUTE_CHECK, 0, "", 35.0, 30
            )
            == EnumVerificationOutcome.VERIFICATION_TIMEOUT
        )

    def test_unavailable_target_skipped(self) -> None:
        assert (
            classify_verification_outcome(
                EnumVerificationTarget.SKIPPED_NO_MAPPING, 0, "", 1.0, 30
            )
            == EnumVerificationOutcome.SKIPPED_NO_MAPPING
        )


@pytest.mark.unit
class TestRuntimeHealthProbe:
    def test_healthy_container_report_has_zero_total_failed(self) -> None:
        runner = _fake_runtime_runner(
            restart_count="0",
            logs="boot\nAuto-wiring complete: wired=3 skipped=0 failed=0\nready\n",
        )

        report = probe_runtime_health(runner=runner)

        assert report.restart_count == 0
        assert report.auto_wiring_report_total_failed == 0
        assert report.auto_wiring_complete_seen is True
        assert report.auto_wiring_failed_seen is False
        assert report.total_failed == 0
        assert report.failures == ()

    def test_broken_restart_count_fails_report(self) -> None:
        runner = _fake_runtime_runner(
            restart_count="2",
            logs="boot\nAuto-wiring complete: wired=3 skipped=0 failed=0\nready\n",
        )

        report = probe_runtime_health(runner=runner)

        assert report.restart_count == 2
        assert report.total_failed == 1
        assert report.failures == ("RestartCount expected 0, got 2",)

    def test_broken_auto_wiring_failure_marker_fails_report(self) -> None:
        runner = _fake_runtime_runner(
            restart_count="0",
            logs=(
                "boot\n"
                "Auto-wiring failed for 1 contract(s): node_bad\n"
                "Auto-wiring complete: wired=2 skipped=0 failed=1\n"
            ),
        )

        report = probe_runtime_health(runner=runner)

        assert report.restart_count == 0
        assert report.auto_wiring_complete_seen is True
        assert report.auto_wiring_failed_seen is True
        assert report.auto_wiring_report_total_failed == 1
        assert report.total_failed == 2
        assert report.failures == (
            "Auto-wiring report.total_failed expected 0, got 1",
            "Auto-wiring failed marker present in last successful boot logs",
        )

    def test_broken_auto_wiring_total_failed_fails_report(self) -> None:
        runner = _fake_runtime_runner(
            restart_count="0",
            logs="Auto-wiring complete: wired=2 skipped=0 failed=1\n",
        )

        report = probe_runtime_health(runner=runner)

        assert report.auto_wiring_report_total_failed == 1
        assert report.total_failed == 1
        assert report.failures == ("Auto-wiring report.total_failed expected 0, got 1",)

    def test_missing_auto_wiring_complete_marker_fails_report(self) -> None:
        runner = _fake_runtime_runner(restart_count="0", logs="boot\nready\n")

        report = probe_runtime_health(runner=runner)

        assert report.auto_wiring_complete_seen is False
        assert report.total_failed == 1
        assert report.failures == (
            "Auto-wiring complete marker missing from last 200 log lines",
        )

    def test_runner_uses_docker_inspect_and_tail_200_logs(self) -> None:
        calls: list[tuple[str, ...]] = []
        runner = _fake_runtime_runner(
            restart_count="0",
            logs="Auto-wiring complete: wired=1 skipped=0 failed=0\n",
            calls=calls,
        )

        probe_runtime_health(runner=runner)

        assert calls == [
            (
                "docker",
                "inspect",
                "--format={{.RestartCount}}",
                "omnibase-infra-omninode-runtime",
            ),
            (
                "docker",
                "logs",
                "--tail",
                "200",
                "omnibase-infra-omninode-runtime",
            ),
        ]

    def test_run_command_returns_structured_timeout_failure(self, monkeypatch) -> None:
        def timeout_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(
                cmd=("docker", "logs"),
                timeout=15,
                output="partial stdout",
                stderr=None,
            )

        monkeypatch.setattr(subprocess, "run", timeout_run)

        result = _run_command(("docker", "logs"))

        assert result.returncode == 124
        assert result.stdout == "partial stdout"
        assert result.stderr == "partial stdout"

    def test_run_command_returns_structured_os_error_failure(self, monkeypatch) -> None:
        def os_error_run(*args, **kwargs):
            raise FileNotFoundError("docker")

        monkeypatch.setattr(subprocess, "run", os_error_run)

        result = _run_command(("docker", "inspect"))

        assert result.returncode == 127
        assert result.stdout == ""
        assert result.stderr == "docker"


@pytest.mark.unit
class TestModelPrLifecycleStartCommand:
    def test_default_verify_false(self) -> None:
        from uuid import uuid4

        from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
            ModelPrLifecycleStartCommand,
        )

        cmd = ModelPrLifecycleStartCommand(correlation_id=uuid4(), run_id="test-run")
        assert cmd.verify is False

    def test_verify_fields_set(self) -> None:
        from uuid import uuid4

        from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
            ModelPrLifecycleStartCommand,
        )

        cmd = ModelPrLifecycleStartCommand(
            correlation_id=uuid4(),
            run_id="test-run",
            verify=True,
            verify_timeout_seconds=60,
        )
        assert cmd.verify is True
        assert cmd.verify_timeout_seconds == 60

    def test_verify_timeout_minimum(self) -> None:
        from uuid import uuid4

        from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
            ModelPrLifecycleStartCommand,
        )

        with pytest.raises(ValidationError):
            ModelPrLifecycleStartCommand(
                correlation_id=uuid4(), run_id="test-run", verify_timeout_seconds=0
            )


def _fake_runtime_runner(
    *,
    restart_count: str,
    logs: str,
    calls: list[tuple[str, ...]] | None = None,
):
    def runner(command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        if calls is not None:
            calls.append(command)
        if command[:2] == ("docker", "inspect"):
            return subprocess.CompletedProcess(command, 0, stdout=restart_count)
        if command[:2] == ("docker", "logs"):
            return subprocess.CompletedProcess(command, 0, stdout=logs)
        return subprocess.CompletedProcess(command, 127, stderr="unexpected command")

    return runner
