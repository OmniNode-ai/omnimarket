"""Result models for node_integration_test_runner."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_integration_test_runner.models.model_test_runner_request import (
    EnumDIProfile,
)


class EnumTestRunStatus(StrEnum):
    """Status for a single node test run or overall run."""

    # Not reusing EnumChainStatus because that tracks Kafka chain validation
    # (head_topic -> tail_table), not pytest test pass/fail counts.
    # Not reusing EnumSweepStatus because PARTIAL does not map to pytest semantics.
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"  # test collection error / import error
    SKIPPED = "skipped"  # no golden chain module found for node
    DRY_RUN = "dry_run"  # dry_run=True, discovered but not executed


class ModelPerTestDetail(BaseModel):
    """Per-test function result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    test_id: str  # e.g. "test_simple_ticket_created"
    status: EnumTestRunStatus
    duration_ms: float = 0.0
    error_message: str = ""


class ModelNodeTestResult(BaseModel):
    """Test run results for a single node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_name: str
    test_module: str  # dotted module path, e.g. "tests.test_golden_chain_create_ticket"
    total: int
    passed: int
    failed: int
    errored: int
    status: EnumTestRunStatus
    per_test: list[ModelPerTestDetail] = Field(default_factory=list)
    error_message: str = ""


class ModelIntegrationTestRunnerResult(BaseModel):
    """Aggregate result for the full test runner run."""

    model_config = ConfigDict(extra="forbid")

    profile: EnumDIProfile
    nodes_run: int
    nodes_passed: int
    nodes_failed: int
    overall_status: EnumTestRunStatus
    node_results: list[ModelNodeTestResult] = Field(default_factory=list)
    discovery_errors: list[str] = Field(default_factory=list)
