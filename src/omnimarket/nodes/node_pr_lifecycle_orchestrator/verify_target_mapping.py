# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Changed-file-to-verification-target mapping for PR lifecycle verify phase (OMN-7742)."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

RUNTIME_HEALTH_CONTAINER_NAME = "omnibase-infra-omninode-runtime"
AUTO_WIRING_COMPLETE_MARKER = "Auto-wiring complete"
AUTO_WIRING_FAILED_MARKER = "Auto-wiring failed"
RUNTIME_HEALTH_DOCKER_TIMEOUT_SECONDS = 15


class EnumVerificationTarget(StrEnum):
    RUNTIME_HEALTH = "runtime-health"
    PROJECTION_ROW_CHECK = "PROJECTION_ROW_CHECK"
    PROJECTION_SINK_CHECK = "PROJECTION_SINK_CHECK"
    API_ROUTE_CHECK = "API_ROUTE_CHECK"
    DB_MIGRATION_CHECK = "DB_MIGRATION_CHECK"
    KAFKA_TOPIC_CHECK = "KAFKA_TOPIC_CHECK"
    SKIPPED_NO_MAPPING = "SKIPPED_NO_MAPPING"


class EnumVerificationOutcome(StrEnum):
    MERGED = "MERGED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    VERIFICATION_UNAVAILABLE = "VERIFICATION_UNAVAILABLE"
    VERIFICATION_TIMEOUT = "VERIFICATION_TIMEOUT"
    VERIFICATION_TOOL_ERROR = "VERIFICATION_TOOL_ERROR"
    SKIPPED_NO_MAPPING = "SKIPPED_NO_MAPPING"
    SKIPPED_BY_POLICY = "SKIPPED_BY_POLICY"


RUNTIME_HEALTH_RULES: list[re.Pattern[str]] = [
    re.compile(r"(^|.*/)runtime/auto_wiring/"),
    re.compile(r"(^|.*/)auto_wiring/"),
    re.compile(r"(^|.*/)service_kernel\.py$"),
    re.compile(r"(^|.*/)handlers/handler_[^/]+\.py$"),
]

TARGET_RULES: list[tuple[re.Pattern[str], EnumVerificationTarget]] = [
    (
        re.compile(r"^src/(.*/)?projection.*\.py$"),
        EnumVerificationTarget.PROJECTION_ROW_CHECK,
    ),
    (
        re.compile(r"^src/(.*/)?projector.*\.py$"),
        EnumVerificationTarget.PROJECTION_ROW_CHECK,
    ),
    (
        re.compile(r"^src/(.*/)?handler.*\.py$"),
        EnumVerificationTarget.PROJECTION_SINK_CHECK,
    ),
    (
        re.compile(r"^src/(.*/)?route.*\.py$"),
        EnumVerificationTarget.API_ROUTE_CHECK,
    ),
    (
        re.compile(r"^src/(.*/)?api.*\.py$"),
        EnumVerificationTarget.API_ROUTE_CHECK,
    ),
    (re.compile(r"^pages/api/"), EnumVerificationTarget.API_ROUTE_CHECK),
    (re.compile(r"^drizzle/"), EnumVerificationTarget.DB_MIGRATION_CHECK),
    (re.compile(r"^migrations/"), EnumVerificationTarget.DB_MIGRATION_CHECK),
    (re.compile(r"(^|.*/)topics\.yaml$"), EnumVerificationTarget.KAFKA_TOPIC_CHECK),
    (re.compile(r"(^|.*/)contract\.yaml$"), EnumVerificationTarget.KAFKA_TOPIC_CHECK),
]


class ModelRuntimeHealthProbeReport(BaseModel):
    """Structured runtime-health probe result for merge-sweep verification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target: EnumVerificationTarget = EnumVerificationTarget.RUNTIME_HEALTH
    container_name: str = RUNTIME_HEALTH_CONTAINER_NAME
    restart_count: int | None = Field(default=None, ge=0)
    auto_wiring_report_total_failed: int | None = Field(default=None, ge=0)
    auto_wiring_complete_seen: bool = False
    auto_wiring_failed_seen: bool = False
    total_failed: int = Field(default=0, ge=0)
    failures: tuple[str, ...] = ()


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def map_changed_files_to_target(changed_files: list[str]) -> EnumVerificationTarget:
    if requires_runtime_health(changed_files):
        return EnumVerificationTarget.RUNTIME_HEALTH

    for path in changed_files:
        for pattern, target in TARGET_RULES:
            if pattern.search(path):
                return target
    return EnumVerificationTarget.SKIPPED_NO_MAPPING


def requires_runtime_health(changed_files: list[str]) -> bool:
    return any(
        pattern.search(path)
        for path in changed_files
        for pattern in RUNTIME_HEALTH_RULES
    )


def classify_verification_outcome(
    target: EnumVerificationTarget,
    exit_code: int,
    stdout: str,
    elapsed_seconds: float,
    timeout_seconds: int,
) -> EnumVerificationOutcome:
    if target == EnumVerificationTarget.SKIPPED_NO_MAPPING:
        return EnumVerificationOutcome.SKIPPED_NO_MAPPING

    if elapsed_seconds >= timeout_seconds:
        return EnumVerificationOutcome.VERIFICATION_TIMEOUT

    if exit_code != 0:
        return EnumVerificationOutcome.VERIFICATION_FAILED

    return EnumVerificationOutcome.MERGED


def probe_runtime_health(
    *,
    runner: CommandRunner | None = None,
    container_name: str = RUNTIME_HEALTH_CONTAINER_NAME,
) -> ModelRuntimeHealthProbeReport:
    """Probe runtime container health without requiring live Docker in tests."""

    command_runner = runner or _run_command
    failures: list[str] = []
    restart_count: int | None = None

    inspect_result = command_runner(
        ("docker", "inspect", "--format={{.RestartCount}}", container_name)
    )
    if inspect_result.returncode != 0:
        failures.append(
            f"docker inspect RestartCount failed: {_command_error(inspect_result)}"
        )
    else:
        restart_text = inspect_result.stdout.strip()
        try:
            restart_count = int(restart_text)
        except ValueError:
            failures.append(
                f"docker inspect RestartCount returned non-integer value: {restart_text!r}"
            )
        else:
            if restart_count != 0:
                failures.append(f"RestartCount expected 0, got {restart_count}")

    logs_result = command_runner(("docker", "logs", "--tail", "200", container_name))
    logs = logs_result.stdout if logs_result.returncode == 0 else ""
    if logs_result.returncode != 0:
        failures.append(f"docker logs tail failed: {_command_error(logs_result)}")

    auto_wiring_complete_seen = AUTO_WIRING_COMPLETE_MARKER in logs
    auto_wiring_report_total_failed = _auto_wiring_report_total_failed(logs)
    auto_wiring_failed_seen = AUTO_WIRING_FAILED_MARKER in logs

    if not auto_wiring_complete_seen:
        failures.append("Auto-wiring complete marker missing from last 200 log lines")
    if (
        auto_wiring_report_total_failed is not None
        and auto_wiring_report_total_failed != 0
    ):
        failures.append(
            "Auto-wiring report.total_failed expected 0, "
            f"got {auto_wiring_report_total_failed}"
        )
    if auto_wiring_failed_seen:
        failures.append(
            "Auto-wiring failed marker present in last successful boot logs"
        )

    return ModelRuntimeHealthProbeReport(
        container_name=container_name,
        restart_count=restart_count,
        auto_wiring_report_total_failed=auto_wiring_report_total_failed,
        auto_wiring_complete_seen=auto_wiring_complete_seen,
        auto_wiring_failed_seen=auto_wiring_failed_seen,
        total_failed=len(failures),
        failures=tuple(failures),
    )


def _run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=RUNTIME_HEALTH_DOCKER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        detail = (
            stderr.strip()
            or stdout.strip()
            or f"timeout after {RUNTIME_HEALTH_DOCKER_TIMEOUT_SECONDS}s"
        )
        return subprocess.CompletedProcess(command, 124, stdout=stdout, stderr=detail)
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, stdout="", stderr=str(exc))


def _command_error(result: subprocess.CompletedProcess[str]) -> str:
    stderr = result.stderr.strip()
    stdout = result.stdout.strip()
    return stderr or stdout or f"exit_code={result.returncode}"


def _auto_wiring_report_total_failed(logs: str) -> int | None:
    complete_lines = [
        line for line in logs.splitlines() if AUTO_WIRING_COMPLETE_MARKER in line
    ]
    if not complete_lines:
        return None

    match = re.search(r"\bfailed=(\d+)\b", complete_lines[-1])
    if match is None:
        return None
    return int(match.group(1))
