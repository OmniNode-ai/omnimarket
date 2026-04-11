"""HandlerIntegrationTestRunner — discovers and runs golden chain tests with DI profile swap."""

from __future__ import annotations

import logging

from omnimarket.nodes.node_integration_test_runner.discovery import (
    discover_node_test_modules,
)
from omnimarket.nodes.node_integration_test_runner.models.model_test_runner_request import (
    ModelIntegrationTestRunnerRequest,
)
from omnimarket.nodes.node_integration_test_runner.models.model_test_runner_result import (
    EnumTestRunStatus,
    ModelIntegrationTestRunnerResult,
    ModelNodeTestResult,
)
from omnimarket.nodes.node_integration_test_runner.pytest_runner import run_node_tests

logger = logging.getLogger(__name__)


class HandlerIntegrationTestRunner:
    """ONEX node handler — runs golden chain tests under a configurable DI profile.

    The key innovation: test code never changes. Only the DI container bindings
    (event bus, client factories) swap per profile:
      - local   : EventBusInmemory + file state + stub clients
      - staging : EventBusKafka + real Postgres + real Linear (.201)
      - production: same bindings against production endpoints
    """

    def handle(
        self, request: ModelIntegrationTestRunnerRequest
    ) -> ModelIntegrationTestRunnerResult:
        """Run golden chain tests for all nodes matching the request filters.

        Args:
            request: Profile selection, --feature / --all flags, dry_run.

        Returns:
            Structured results: node count, pass/fail, per-test details.
        """
        mappings = discover_node_test_modules(
            package_root="omnimarket",
            feature=request.feature,
        )

        if not mappings:
            return ModelIntegrationTestRunnerResult(
                profile=request.profile,
                nodes_run=0,
                nodes_passed=0,
                nodes_failed=0,
                overall_status=EnumTestRunStatus.PASS,
            )

        node_results: list[ModelNodeTestResult] = []
        discovery_errors: list[str] = []

        for mapping in mappings:
            logger.info(
                "[integration-test-runner] running node=%s profile=%s",
                mapping.node_name,
                request.profile,
            )
            try:
                result = run_node_tests(
                    mapping=mapping,
                    profile=request.profile,
                    dry_run=request.dry_run,
                    timeout_s=request.timeout_per_node_s,
                )
            except Exception as exc:
                logger.exception(
                    "[integration-test-runner] unexpected error for node=%s",
                    mapping.node_name,
                )
                result = ModelNodeTestResult(
                    node_name=mapping.node_name,
                    test_module=mapping.test_module_path or "",
                    total=0,
                    passed=0,
                    failed=0,
                    errored=1,
                    status=EnumTestRunStatus.ERROR,
                    error_message=str(exc),
                )
                discovery_errors.append(f"{mapping.node_name}: {exc}")
            node_results.append(result)

        runnable = [
            r
            for r in node_results
            if r.status not in (EnumTestRunStatus.SKIPPED, EnumTestRunStatus.DRY_RUN)
        ]
        passed = sum(1 for r in runnable if r.status == EnumTestRunStatus.PASS)
        failed = len(runnable) - passed

        if request.dry_run:
            overall = EnumTestRunStatus.DRY_RUN
        elif not runnable:
            overall = EnumTestRunStatus.SKIPPED
        elif failed > 0:
            overall = EnumTestRunStatus.FAIL
        else:
            overall = EnumTestRunStatus.PASS

        return ModelIntegrationTestRunnerResult(
            profile=request.profile,
            nodes_run=len(runnable),
            nodes_passed=passed,
            nodes_failed=failed,
            overall_status=overall,
            node_results=node_results,
            discovery_errors=discovery_errors,
        )
