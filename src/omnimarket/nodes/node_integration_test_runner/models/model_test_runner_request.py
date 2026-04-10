"""Request model for node_integration_test_runner."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EnumDIProfile(StrEnum):
    """DI binding profile to use when running tests."""

    LOCAL = "local"  # EventBusInmemory + file state + stub clients
    STAGING = "staging"  # EventBusKafka + real Postgres + real Linear (.201)
    PRODUCTION = "production"  # same as staging but against prod endpoints


class ModelIntegrationTestRunnerRequest(BaseModel):
    """Input for the integration test runner handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile: EnumDIProfile = EnumDIProfile.LOCAL
    feature: str | None = None  # Run only this node's tests (e.g. "node_create_ticket")
    all_nodes: bool = False  # Run all discovered nodes' tests
    timeout_per_node_s: int = 120
    dry_run: bool = False  # Discover tests but do not run them
