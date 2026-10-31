# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ten nodes read their timeouts from their contract.yaml, with every value as before."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from omnimarket.handlers.node_timeouts_reader import load_node_timeouts
from omnimarket.nodes.node_review_finding_veto_effect.handlers import (
    adapter_git_diff,
    adapter_git_head,
)
from omnimarket.nodes.node_runtime_sweep import broker_probe
from omnimarket.nodes.node_secret_store_effect.adapters.handler_infisical_http import (
    HandlerInfisicalHttp,
)
from omnimarket.nodes.node_session_bootstrap.handlers import handler_session_bootstrap
from omnimarket.nodes.node_shadow_review_effect.handlers.shadow_effects import (
    CanonicalCloneDiffSource,
)
from omnimarket.nodes.node_silent_failure_alarm_compute.handlers import (
    handler_placement_share_alarm,
)
from omnimarket.nodes.node_swarm_endpoint_health_effect.handlers import (
    handler_swarm_endpoint_health,
)
from omnimarket.nodes.node_swarm_fleet_discovery_effect.handlers import (
    handler_swarm_fleet_discovery,
)
from omnimarket.nodes.node_topic_activity_sampler_effect.handlers import (
    _kafka_topic_activity_reader,
)

# node -> {declared name: the value the node's code held as a literal before it moved}
DECLARED: dict[str, dict[str, float]] = {
    "node_review_finding_veto_effect": {
        "git_diff_timeout_seconds": 120,
        "git_head_timeout_seconds": 60,
    },
    "node_runtime_sweep": {
        "admin_request_timeout_seconds": 10.0,
        "admin_result_timeout_seconds": 15.0,
    },
    "node_session_bootstrap": {
        "pr_list_timeout_seconds": 30,
        "golden_chain_timeout_seconds": 120,
        "pre_commit_timeout_seconds": 120,
        "overseer_verify_timeout_seconds": 120,
        "build_dispatch_pulse_budget_seconds": 300,
        "merge_sweep_budget_seconds": 600,
        "overseer_verify_budget_seconds": 120,
        "contract_verify_budget_seconds": 120,
    },
    "node_session_orchestrator": {
        "pr_list_timeout_seconds": 30,
        "postgres_probe_timeout_seconds": 10,
        "golden_chain_timeout_seconds": 60,
        "docker_ps_timeout_seconds": 15,
        "plugin_list_timeout_seconds": 15,
        "deploy_agent_probe_timeout_seconds": 15,
        "redpanda_health_timeout_seconds": 20,
        "git_rev_list_timeout_seconds": 10,
        "linear_graphql_timeout_seconds": 15,
    },
    "node_shadow_review_effect": {
        "git_timeout_seconds": 120.0,
        "refresh_timeout_seconds": 300,
    },
    "node_silent_failure_alarm_compute": {"fill_interval_seconds": 1200},
    "node_swarm_endpoint_health_effect": {"request_timeout_seconds": 30.0},
    "node_swarm_fleet_discovery_effect": {"request_timeout_seconds": 10.0},
    "node_topic_activity_sampler_effect": {"fetch_timeout_seconds": 5.0},
}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("node_name", "declared"), sorted(DECLARED.items()), ids=sorted(DECLARED)
)
def test_contract_declares_every_duration_with_its_former_value(
    node_name: str, declared: dict[str, float]
) -> None:
    timeouts = load_node_timeouts(node_name)
    assert dict(timeouts.seconds) == declared
    for name, value in declared.items():
        assert timeouts.get(name) == value


@pytest.mark.unit
@pytest.mark.parametrize("node_name", sorted(DECLARED))
def test_an_undeclared_name_stops_the_node(node_name: str) -> None:
    with pytest.raises(KeyError, match="declares no timeout named 'nope'"):
        load_node_timeouts(node_name).get("nope")


@pytest.mark.unit
def test_module_constants_come_from_the_contract() -> None:
    assert adapter_git_diff._GIT_TIMEOUT_SECONDS == 120
    assert adapter_git_head._GIT_TIMEOUT_SECONDS == 60
    assert broker_probe._ADMIN_REQUEST_TIMEOUT_S == 10.0
    assert broker_probe._ADMIN_RESULT_TIMEOUT_S == 15.0
    assert handler_placement_share_alarm.FILL_INTERVAL_MINUTES == 20
    assert handler_swarm_endpoint_health._DEFAULT_TIMEOUT_SECONDS == 30.0
    assert handler_swarm_fleet_discovery._DEFAULT_TIMEOUT_SECONDS == 10.0
    assert _kafka_topic_activity_reader._FETCH_TIMEOUT_SECONDS == 5.0


@pytest.mark.unit
def test_cron_budgets_come_from_the_contract_in_milliseconds() -> None:
    budgets = {
        spec.cron_name: spec.timeout_budget_ms
        for spec in handler_session_bootstrap._REQUIRED_CRONS
    }
    assert budgets == {
        "build-dispatch-pulse": 300000,
        "merge-sweep": 600000,
        "overseer-verify": 120000,
        "contract-verify": 120000,
    }


async def _no_exchange(*args: Any, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    """An HTTP exchange that is never called; construction alone is under test."""
    raise AssertionError("not called")


def _infisical(**kwargs: Any) -> HandlerInfisicalHttp:
    return HandlerInfisicalHttp(
        address="http://secrets.invalid",
        project_id="p",
        environment="e",
        client_id=SecretStr("id"),
        client_secret=SecretStr("secret"),
        exchange=_no_exchange,
        **kwargs,
    )


@pytest.mark.unit
def test_constructor_defaults_come_from_the_contract() -> None:
    source = CanonicalCloneDiffSource(Path("home"), Path("refresh.sh"))
    assert source._refresh_timeout_s == 300


@pytest.mark.unit
def test_the_secret_store_call_timeout_has_no_default_in_code() -> None:
    with pytest.raises(TypeError, match="timeout_seconds"):
        _infisical()


@pytest.mark.unit
def test_an_injected_duration_wins_over_the_contract() -> None:
    assert _infisical(timeout_seconds=1.5)._timeout == 1.5
    source = CanonicalCloneDiffSource(
        Path("home"), Path("refresh.sh"), refresh_timeout_s=7
    )
    assert source._refresh_timeout_s == 7
