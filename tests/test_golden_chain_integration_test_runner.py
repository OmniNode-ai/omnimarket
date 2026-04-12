"""Golden chain tests for node_integration_test_runner."""

from __future__ import annotations

import subprocess
import sys

import pytest

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)
from omnimarket.nodes.node_integration_test_runner.di_profiles import (
    build_conftest_plugin_for_profile,
    build_event_bus_for_profile,
)
from omnimarket.nodes.node_integration_test_runner.discovery import (
    NodeTestMapping,
    discover_node_test_modules,
)
from omnimarket.nodes.node_integration_test_runner.handlers.handler_integration_test_runner import (
    HandlerIntegrationTestRunner,
)
from omnimarket.nodes.node_integration_test_runner.models.model_test_runner_request import (
    EnumDIProfile,
    ModelIntegrationTestRunnerRequest,
)
from omnimarket.nodes.node_integration_test_runner.models.model_test_runner_result import (
    EnumTestRunStatus,
    ModelIntegrationTestRunnerResult,
    ModelNodeTestResult,
)
from omnimarket.nodes.node_integration_test_runner.pytest_runner import run_node_tests


class TestIntegrationTestRunnerModels:
    def test_request_local_profile_defaults(self) -> None:
        req = ModelIntegrationTestRunnerRequest(profile=EnumDIProfile.LOCAL)
        assert req.profile == EnumDIProfile.LOCAL
        assert req.feature is None
        assert req.all_nodes is False

    def test_request_feature_flag(self) -> None:
        req = ModelIntegrationTestRunnerRequest(
            profile=EnumDIProfile.LOCAL, feature="node_create_ticket"
        )
        assert req.feature == "node_create_ticket"

    def test_result_pass_status(self) -> None:
        node_result = ModelNodeTestResult(
            node_name="node_create_ticket",
            test_module="tests.test_golden_chain_create_ticket",
            total=3,
            passed=3,
            failed=0,
            errored=0,
            status=EnumTestRunStatus.PASS,
            per_test=[],
        )
        result = ModelIntegrationTestRunnerResult(
            profile=EnumDIProfile.LOCAL,
            nodes_run=1,
            nodes_passed=1,
            nodes_failed=0,
            overall_status=EnumTestRunStatus.PASS,
            node_results=[node_result],
        )
        assert result.overall_status == EnumTestRunStatus.PASS
        assert result.nodes_passed == 1


class TestNodeDiscovery:
    def test_discover_returns_mapping_for_all_registered_nodes(self) -> None:
        """discover_node_test_modules reads onex.nodes EPs and finds test modules."""
        mappings = discover_node_test_modules(package_root="omnimarket")
        node_names = {m.node_name for m in mappings}
        assert "node_create_ticket" in node_names

    def test_mapping_has_test_module_for_node_with_golden_chain(self) -> None:
        mappings = discover_node_test_modules(package_root="omnimarket")
        by_name = {m.node_name: m for m in mappings}
        m = by_name["node_create_ticket"]
        assert m.test_module_path is not None
        assert "test_golden_chain_create_ticket" in m.test_module_path

    def test_node_without_golden_chain_returns_none_module(self) -> None:
        """Nodes without a golden chain file get test_module_path=None."""
        mappings = discover_node_test_modules(package_root="omnimarket")
        by_name = {m.node_name: m for m in mappings}
        # node_retention_cleanup has no golden chain test
        if "node_retention_cleanup" in by_name:
            assert by_name["node_retention_cleanup"].test_module_path is None

    def test_feature_filter_limits_to_one_node(self) -> None:
        mappings = discover_node_test_modules(
            package_root="omnimarket", feature="node_dod_verify"
        )
        assert len(mappings) == 1
        assert mappings[0].node_name == "node_dod_verify"


class TestDIProfiles:
    def test_local_profile_returns_inmemory_bus(self) -> None:
        bus = build_event_bus_for_profile(EnumDIProfile.LOCAL)
        assert isinstance(bus, EventBusInmemory)

    def test_staging_profile_returns_protocol_compatible_bus(self) -> None:
        """Staging returns an object satisfying ProtocolEventBusPublisher.
        In test env without KAFKA_BOOTSTRAP_SERVERS, falls back to EventBusInmemory."""
        import os

        os.environ.pop("KAFKA_BOOTSTRAP_SERVERS", None)
        bus = build_event_bus_for_profile(EnumDIProfile.STAGING)
        assert isinstance(bus, ProtocolEventBusPublisher | EventBusInmemory)

    def test_local_profile_env_vars_injected_into_conftest_plugin(self) -> None:
        """build_conftest_plugin_for_profile(LOCAL) returns a pytest plugin
        whose event_bus fixture returns EventBusInmemory."""
        plugin = build_conftest_plugin_for_profile(EnumDIProfile.LOCAL)
        assert hasattr(plugin, "event_bus")


@pytest.mark.unit
class TestPytestRunnerAdapter:
    def test_run_node_tests_returns_node_result(self) -> None:
        """run_node_tests executes tests for one node and returns ModelNodeTestResult."""
        mapping = NodeTestMapping(
            node_name="node_create_ticket",
            handler_class_path="omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket:HandlerCreateTicket",
            test_module_path="tests.test_golden_chain_create_ticket",
        )
        result = run_node_tests(mapping=mapping, profile=EnumDIProfile.LOCAL)
        assert isinstance(result, ModelNodeTestResult)
        assert result.node_name == "node_create_ticket"
        assert result.total >= 1
        assert result.status == EnumTestRunStatus.PASS
        assert result.passed == result.total

    def test_run_node_tests_no_module_returns_skipped(self) -> None:
        mapping = NodeTestMapping(
            node_name="node_has_no_tests",
            handler_class_path="omnimarket.nodes.node_has_no_tests:Handler",
            test_module_path=None,
        )
        result = run_node_tests(mapping=mapping, profile=EnumDIProfile.LOCAL)
        assert result.status == EnumTestRunStatus.SKIPPED
        assert result.total == 0

    def test_dry_run_returns_dry_run_status(self) -> None:
        mapping = NodeTestMapping(
            node_name="node_create_ticket",
            handler_class_path="omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket:HandlerCreateTicket",
            test_module_path="tests.test_golden_chain_create_ticket",
        )
        result = run_node_tests(
            mapping=mapping, profile=EnumDIProfile.LOCAL, dry_run=True
        )
        assert result.status == EnumTestRunStatus.DRY_RUN
        assert result.total == 0


@pytest.mark.unit
class TestHandlerIntegrationTestRunner:
    def test_single_feature_local_profile_passes(self) -> None:
        """--feature node_create_ticket --profile local runs tests, all pass."""
        handler = HandlerIntegrationTestRunner()
        req = ModelIntegrationTestRunnerRequest(
            profile=EnumDIProfile.LOCAL,
            feature="node_create_ticket",
        )
        result = handler.handle(req)
        assert isinstance(result, ModelIntegrationTestRunnerResult)
        assert result.nodes_run == 1
        assert result.nodes_passed == 1
        assert result.nodes_failed == 0
        assert result.overall_status == EnumTestRunStatus.PASS
        assert result.node_results[0].node_name == "node_create_ticket"
        assert result.node_results[0].total >= 3

    def test_dry_run_discovers_without_running(self) -> None:
        handler = HandlerIntegrationTestRunner()
        req = ModelIntegrationTestRunnerRequest(
            profile=EnumDIProfile.LOCAL,
            feature="node_dod_verify",
            dry_run=True,
        )
        result = handler.handle(req)
        # In dry_run mode, nodes are discovered but not executed (nodes_run=0)
        assert result.nodes_run == 0
        assert result.overall_status == EnumTestRunStatus.DRY_RUN
        assert len(result.node_results) == 1
        assert result.node_results[0].status == EnumTestRunStatus.DRY_RUN
        assert result.node_results[0].total == 0

    def test_unknown_feature_returns_empty_result(self) -> None:
        handler = HandlerIntegrationTestRunner()
        req = ModelIntegrationTestRunnerRequest(
            profile=EnumDIProfile.LOCAL,
            feature="node_does_not_exist_xyz",
        )
        result = handler.handle(req)
        assert result.nodes_run == 0
        assert result.overall_status == EnumTestRunStatus.PASS


class TestCLI:
    def test_cli_feature_dry_run_exits_zero(self) -> None:
        """CLI --feature node_create_ticket --dry-run exits 0 and prints JSON."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "omnimarket.nodes.node_integration_test_runner.cli",
                "--feature",
                "node_create_ticket",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        import json

        data = json.loads(result.stdout)
        assert data["overall_status"] == "dry_run"

    def test_cli_unknown_feature_exits_zero_empty_result(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "omnimarket.nodes.node_integration_test_runner.cli",
                "--feature",
                "node_does_not_exist_xyz",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        import json

        data = json.loads(result.stdout)
        assert data["nodes_run"] == 0
