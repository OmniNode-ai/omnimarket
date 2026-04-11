"""Programmatic pytest runner adapter — runs one node's tests with injected DI plugin."""

from __future__ import annotations

import io
import logging
import sys
import time
from typing import Any

import pytest

from omnimarket.nodes.node_integration_test_runner.di_profiles import (
    build_conftest_plugin_for_profile,
)
from omnimarket.nodes.node_integration_test_runner.discovery import NodeTestMapping
from omnimarket.nodes.node_integration_test_runner.models.model_test_runner_request import (
    EnumDIProfile,
)
from omnimarket.nodes.node_integration_test_runner.models.model_test_runner_result import (
    EnumTestRunStatus,
    ModelNodeTestResult,
    ModelPerTestDetail,
)

logger = logging.getLogger(__name__)


class _ResultCollector:
    """Minimal pytest plugin that collects per-test outcomes."""

    def __init__(self) -> None:
        self.per_test: list[ModelPerTestDetail] = []
        self.total = 0
        self.passed = 0
        self.failed = 0
        self.errored = 0

    def pytest_runtest_logreport(self, report: Any) -> None:
        # Count each test item exactly once.
        # - setup failure: collection error, counts as ERROR
        # - call failure/pass: the actual test result
        # - teardown failure: append a separate ERROR entry (test ran but cleanup failed)
        if report.when == "setup" and report.failed:
            self.total += 1
            self.errored += 1
            status = EnumTestRunStatus.ERROR
        elif report.when == "call":
            self.total += 1
            if report.passed:
                status = EnumTestRunStatus.PASS
                self.passed += 1
            else:
                status = EnumTestRunStatus.FAIL
                self.failed += 1
        elif report.when == "teardown" and report.failed:
            # Teardown failure: do not increment total (test already counted), add error entry
            self.errored += 1
            self.per_test.append(
                ModelPerTestDetail(
                    test_id=report.nodeid.split("::")[-1] + "[teardown]",
                    status=EnumTestRunStatus.ERROR,
                    duration_ms=round(getattr(report, "duration", 0.0) * 1000, 2),
                    error_message=str(report.longrepr),
                )
            )
            return
        else:
            return

        self.per_test.append(
            ModelPerTestDetail(
                test_id=report.nodeid.split("::")[-1],
                status=status,
                duration_ms=round(getattr(report, "duration", 0.0) * 1000, 2),
                error_message=str(report.longrepr) if report.failed else "",
            )
        )


def run_node_tests(
    mapping: NodeTestMapping,
    profile: EnumDIProfile,
    dry_run: bool = False,
    timeout_s: int = 120,
) -> ModelNodeTestResult:
    """Run golden chain tests for one node with the given DI profile.

    Args:
        mapping: NodeTestMapping from discover_node_test_modules().
        profile: Which DI profile to inject.
        dry_run: If True, discover tests but do not execute them.
        timeout_s: Per-node pytest timeout in seconds.

    Returns:
        ModelNodeTestResult with per-test details.

    Note:
        pytest.main() re-uses the current process. When called from within an existing
        pytest session (e.g. running our own test suite) this can corrupt global pytest
        state. run_node_tests() is designed for CLI use and for the handler called outside
        a test session. Do not call it from within a pytest test function — use subprocess
        isolation in that case.
    """
    if mapping.test_module_path is None:
        return ModelNodeTestResult(
            node_name=mapping.node_name,
            test_module=mapping.handler_class_path,
            total=0,
            passed=0,
            failed=0,
            errored=0,
            status=EnumTestRunStatus.SKIPPED,
        )

    if dry_run:
        return ModelNodeTestResult(
            node_name=mapping.node_name,
            test_module=mapping.test_module_path,
            total=0,
            passed=0,
            failed=0,
            errored=0,
            status=EnumTestRunStatus.DRY_RUN,
        )

    collector = _ResultCollector()
    di_plugin = build_conftest_plugin_for_profile(profile)

    # Convert dotted module path to file path understood by pytest
    # e.g. "tests.test_golden_chain_create_ticket" -> "tests/test_golden_chain_create_ticket.py"
    test_path = mapping.test_module_path.replace(".", "/") + ".py"

    # Suppress pytest's own progress output so it doesn't pollute the caller's stdout.
    # Test output (print/logging) is captured separately via --capture=sys.
    _devnull = io.StringIO()
    _orig_stdout, _orig_stderr = sys.stdout, sys.stderr
    sys.stdout = _devnull
    sys.stderr = _devnull
    start = time.monotonic()
    try:
        exit_code = pytest.main(
            args=[
                test_path,
                "--tb=short",
                "-q",
                "--no-header",
            ],
            plugins=[collector, di_plugin],
        )
    finally:
        sys.stdout = _orig_stdout
        sys.stderr = _orig_stderr
    duration_ms = round((time.monotonic() - start) * 1000, 2)
    logger.debug(
        "[pytest_runner] node=%s profile=%s exit_code=%s duration_ms=%.0f",
        mapping.node_name,
        profile,
        exit_code,
        duration_ms,
    )

    if collector.errored > 0 and collector.passed == 0 and collector.failed == 0:
        overall = EnumTestRunStatus.ERROR
    elif collector.failed > 0 or collector.errored > 0:
        overall = EnumTestRunStatus.FAIL
    else:
        overall = EnumTestRunStatus.PASS

    return ModelNodeTestResult(
        node_name=mapping.node_name,
        test_module=mapping.test_module_path,
        total=collector.total,
        passed=collector.passed,
        failed=collector.failed,
        errored=collector.errored,
        status=overall,
        per_test=collector.per_test,
    )
