# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory stand-ins come from a node's binding site, not from the caller (OMN-21695).

Each node whose entry point or handler used to construct an in-memory bus, store or
transport by hand now takes it from ``container/container_inmemory.py``. Values that
used to be literals at the call site (an environment, a consumer group) are read from
the node contract's ``in_memory_bindings`` section.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.runtime.transport import InMemoryTransport
from omnibase_core.validation.validator_contracts_and_di import (
    ValidatorContractsAndDi,
)
from omnibase_infra.event_bus.event_bus_inmemory import (
    EventBusInmemory as InfraEventBusInmemory,
)

from omnimarket.nodes.node_claude_review_batch_effect.container import (
    container_inmemory as batch_container,
)
from omnimarket.nodes.node_contract_seams_orchestrator.container import (
    container_inmemory as seams_container,
)
from omnimarket.nodes.node_data_verification.container import (
    container_inmemory as data_container,
)
from omnimarket.nodes.node_data_verification.handlers.handler_data_verification import (
    InmemoryDataSource,
)
from omnimarket.nodes.node_integration_test_runner.container import (
    container_inmemory as runner_container,
)
from omnimarket.nodes.node_lab_secret_store_override_effect.container import (
    container_inmemory as secret_container,
)
from omnimarket.nodes.node_merge_sweep_compute.container import (
    container_inmemory as sweep_container,
)
from omnimarket.nodes.node_observability_sink_effect.container import (
    container_inmemory as sink_container,
)
from omnimarket.nodes.node_observability_sink_effect.handlers.handler_observability_sink_effect import (
    InMemoryObservabilitySink,
)
from omnimarket.nodes.node_overnight.container import (
    container_inmemory as overnight_container,
)
from omnimarket.nodes.node_pipeline_fill.container import (
    container_inmemory as fill_container,
)
from omnimarket.nodes.node_pr_landing_orchestrator.container import (
    container_inmemory as landing_container,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.row_store import (
    InMemoryPrLandingRowStore,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.container import (
    container_inmemory as fix_container,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.container import (
    container_inmemory as lifecycle_container,
)
from omnimarket.nodes.node_runtime_deploy_effect.container import (
    container_inmemory as deploy_container,
)
from omnimarket.nodes.node_trajectory_evaluation_effect.container import (
    container_inmemory as trajectory_container,
)
from omnimarket.nodes.node_trajectory_evaluation_effect.evaluators.evaluator_in_memory import (
    EvaluatorInMemory,
)

_REPO = Path(__file__).resolve().parents[3]
_NODES = _REPO / "src" / "omnimarket" / "nodes"

# Every module that held a hand construction before this slice.
_SLICE_MODULES = (
    "node_claude_review_batch_effect/__main__.py",
    "node_contract_seams_orchestrator/handlers/handler_contract_seams_run.py",
    "node_data_verification/handlers/handler_data_verification.py",
    "node_integration_test_runner/di_profiles.py",
    "node_lab_secret_store_override_effect/handlers/handler_lab_secret_store_override_cli.py",
    "node_merge_sweep_compute/handlers/handler_merge_sweep.py",
    "node_observability_sink_effect/handlers/handler_observability_sink_effect.py",
    "node_overnight/handlers/handler_overnight.py",
    "node_pipeline_fill/__main__.py",
    "node_pr_landing_orchestrator/handlers/handler_pr_landing_orchestrator.py",
    "node_pr_lifecycle_fix_effect/handlers/handler_pr_lifecycle_fix.py",
    "node_pr_lifecycle_orchestrator/__main__.py",
    "node_pr_lifecycle_orchestrator/consumer.py",
    "node_runtime_deploy_effect/handlers/handler_runtime_deploy_cli.py",
    "node_trajectory_evaluation_effect/handlers/handler_trajectory_evaluation.py",
)


def _declared(node: str) -> dict[str, str]:
    contract = yaml.safe_load((_NODES / node / "contract.yaml").read_text("utf-8"))
    return contract["in_memory_bindings"]


@pytest.mark.unit
def test_batch_bus_takes_environment_and_group_from_the_contract() -> None:
    declared = _declared("node_claude_review_batch_effect")
    bus = batch_container.bind_event_bus()
    assert isinstance(bus, EventBusInmemory)
    assert (bus.environment, bus.group) == (
        declared["event_bus_environment"],
        declared["event_bus_group"],
    )


@pytest.mark.unit
def test_seams_bus_takes_environment_from_the_contract_and_group_from_the_caller() -> (
    None
):
    declared = _declared("node_contract_seams_orchestrator")
    bus = seams_container.bind_event_bus(group="a-run-group")
    assert isinstance(bus, EventBusInmemory)
    assert (bus.environment, bus.group) == (
        declared["event_bus_environment"],
        "a-run-group",
    )


@pytest.mark.unit
def test_deploy_bus_is_the_infrastructure_bus_the_contract_names() -> None:
    declared = _declared("node_runtime_deploy_effect")
    bus = deploy_container.bind_event_bus()
    assert isinstance(bus, InfraEventBusInmemory)
    assert (bus.environment, bus.group) == (
        declared["event_bus_environment"],
        declared["event_bus_group"],
    )


@pytest.mark.unit
def test_runner_buses_share_the_contract_group() -> None:
    declared = _declared("node_integration_test_runner")
    local = runner_container.bind_local_event_bus()
    fallback = runner_container.bind_fallback_event_bus(environment="staging")
    assert (local.environment, local.group) == (
        declared["local_profile_environment"],
        declared["event_bus_group"],
    )
    assert (fallback.environment, fallback.group) == (
        "staging",
        declared["event_bus_group"],
    )


@pytest.mark.unit
def test_secret_store_transports_share_one_broker_across_the_contract_groups() -> None:
    declared = _declared("node_lab_secret_store_override_effect")
    assert set(declared) == {"publisher_group", "terminal_group"}

    async def round_trip() -> list[bytes]:
        broker = secret_container.bind_broker()
        publisher = secret_container.bind_publisher(broker)
        reader = secret_container.bind_terminal_reader(broker, topics=["a.topic"])
        assert isinstance(publisher, InMemoryTransport)
        try:
            await publisher.send("a.topic", key=None, value=b"payload", headers={})
            await reader.start()
            messages = await reader.poll(max_messages=2, timeout_ms=0)
            return [message.value for message in messages]
        finally:
            await reader.close()
            await publisher.close()

    assert asyncio.run(round_trip()) == [b"payload"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "binder",
    [
        sweep_container.bind_event_bus,
        overnight_container.bind_event_bus,
        fill_container.bind_event_bus,
        lifecycle_container.bind_event_bus,
    ],
)
def test_default_bus_binders_return_a_fresh_bus(
    binder: Callable[[], EventBusInmemory],
) -> None:
    first, second = binder(), binder()
    assert isinstance(first, EventBusInmemory)
    assert first is not second


@pytest.mark.unit
def test_store_binders_return_the_stand_in_their_node_owns() -> None:
    assert isinstance(data_container.bind_data_source(), InmemoryDataSource)
    assert isinstance(sink_container.bind_local_sink(), InMemoryObservabilitySink)
    assert isinstance(landing_container.bind_row_store(), InMemoryPrLandingRowStore)
    assert isinstance(trajectory_container.bind_evaluator(), EvaluatorInMemory)


@pytest.mark.unit
def test_two_strike_store_counts_and_is_not_shared() -> None:
    first = fix_container.bind_two_strike_store()
    second = fix_container.bind_two_strike_store()
    assert first.get_strikes("k") == 0
    assert first.record_failure("k") == 1
    assert first.record_failure("k") == 2
    assert second.get_strikes("k") == 0


@pytest.mark.unit
@pytest.mark.parametrize("relative", _SLICE_MODULES)
def test_slice_module_constructs_no_in_memory_stand_in(relative: str) -> None:
    result = ValidatorContractsAndDi().validate_file(_NODES / relative)
    assert [
        issue.message
        for issue in result.issues
        if "inmemory_outside_injection" in str(issue.code)
    ] == []


@pytest.mark.unit
def test_positive_control_a_hand_construction_is_flagged(tmp_path: Path) -> None:
    sample = tmp_path / "sample_module.py"
    sample.write_text(
        "from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory\n"
        "bus = EventBusInmemory()\n",
        encoding="utf-8",
    )
    result = ValidatorContractsAndDi().validate_file(sample)
    assert [
        issue
        for issue in result.issues
        if "inmemory_outside_injection" in str(issue.code)
    ]
