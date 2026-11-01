# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The handlers of twenty-two nodes are constructed only at their binding sites (OMN-21623).

The contracts_and_di check of the pinned omnibase_core refuses a handler or node class
constructed outside a ``container/container_*.py`` site. Each node below keeps its
construction in that one module and every caller resolves through its factory.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from omnibase_core.models.validation.model_contracts_and_di_facts import (
    ModelContractsAndDiFacts,
)
from omnibase_core.validation.checker_contracts_and_di import check_contracts_and_di
from omnibase_core.validation.validator_contracts_and_di import (
    ValidatorContractsAndDi,
)

_RULE = "handler_hand_construction"
_NODES_ROOT = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"

_SLICE_NODES = (
    "node_skill_dispatch_engine_orchestrator",
    "node_skill_hygiene_validate_compute",
    "node_skill_tree_read_effect",
    "node_slack_publish_effect",
    "node_standup_draft_check_compute",
    "node_task_execution_orchestrator",
    "node_ticket_contract_prerequisite_effect",
    "node_ticket_text_effect",
    "node_topic_archive_effect",
    "node_topic_archive_replay_effect",
    "node_trajectory_evaluation_effect",
    "node_typed_decision_effect",
    "node_umbrella_ticket_compute",
    "node_verification_receipt_generator",
    "node_verification_sweep_orchestrator",
    "node_version_skew_detector",
    "node_volume_config_drift_sweep",
    "node_wave_scheduler_orchestrator",
    "node_wip_admission_compute",
    "node_work_ledger_emit_effect",
    "node_work_ledger_seq_gap_effect",
    "node_worktree_reconcile_effect",
)

# (node, factory name, class the factory returns) for every factory that takes no
# collaborator the test would have to invent.
_ARGUMENTLESS_FACTORIES = (
    (
        "node_skill_dispatch_engine_orchestrator",
        "create_handler_dispatch_engine_router",
        "HandlerDispatchEngineRouter",
    ),
    (
        "node_skill_dispatch_engine_orchestrator",
        "create_handler_rsd_fill",
        "HandlerRsdFill",
    ),
    (
        "node_skill_hygiene_validate_compute",
        "create_handler_skill_hygiene_validate",
        "HandlerSkillHygieneValidate",
    ),
    (
        "node_skill_tree_read_effect",
        "create_handler_skill_tree_read",
        "HandlerSkillTreeRead",
    ),
    (
        "node_slack_publish_effect",
        "create_handler_slack_publish_effect",
        "HandlerSlackPublishEffect",
    ),
    (
        "node_standup_draft_check_compute",
        "create_handler_standup_draft_check",
        "HandlerStandupDraftCheck",
    ),
    (
        "node_task_execution_orchestrator",
        "create_handler_task_execution_orchestrator",
        "HandlerTaskExecutionOrchestrator",
    ),
    (
        "node_task_execution_orchestrator",
        "create_handler_verification_receipt_generator",
        "HandlerVerificationReceiptGenerator",
    ),
    (
        "node_ticket_contract_prerequisite_effect",
        "create_handler_ticket_contract_prerequisite",
        "HandlerTicketContractPrerequisite",
    ),
    (
        "node_ticket_text_effect",
        "create_handler_ticket_text",
        "HandlerTicketText",
    ),
    (
        "node_trajectory_evaluation_effect",
        "create_handler_trajectory_evaluation",
        "HandlerTrajectoryEvaluation",
    ),
    (
        "node_typed_decision_effect",
        "create_handler_typed_decision",
        "HandlerTypedDecision",
    ),
    (
        "node_umbrella_ticket_compute",
        "create_handler_umbrella_ticket",
        "HandlerUmbrellaTicket",
    ),
    (
        "node_verification_receipt_generator",
        "create_handler_verification_receipt_generator",
        "HandlerVerificationReceiptGenerator",
    ),
    (
        "node_verification_sweep_orchestrator",
        "create_handler_verification_sweep_orchestrator",
        "HandlerVerificationSweepOrchestrator",
    ),
    (
        "node_version_skew_detector",
        "create_node_version_skew_detector",
        "NodeVersionSkewDetector",
    ),
    (
        "node_volume_config_drift_sweep",
        "create_node_volume_config_drift_sweep",
        "NodeVolumeConfigDriftSweep",
    ),
    (
        "node_wip_admission_compute",
        "create_handler_wip_admission",
        "HandlerWipAdmission",
    ),
    (
        "node_work_ledger_emit_effect",
        "create_handler_event_emit_effect",
        "HandlerEventEmitEffect",
    ),
)


def _container_module(node: str) -> str:
    return f"omnimarket.nodes.{node}.container.container_{node.removeprefix('node_')}"


def _hand_construction_issues(paths: list[Path]) -> list[str]:
    result = ValidatorContractsAndDi().validate(paths)
    return [
        f"{issue.file_path}:{issue.line_number}"
        for issue in result.issues
        if issue.code == _RULE
    ]


@pytest.mark.unit
@pytest.mark.parametrize("node", _SLICE_NODES)
def test_node_constructs_no_handler_by_hand(node: str) -> None:
    assert (_NODES_ROOT / node / "container").is_dir()
    assert _hand_construction_issues([_NODES_ROOT / node]) == []


@pytest.mark.unit
def test_check_fires_outside_a_binding_site_and_not_at_one() -> None:
    rules = ValidatorContractsAndDi().contract.rules
    facts = ModelContractsAndDiFacts(
        repository="omnimarket",
        tag_source_repositories=frozenset(),
        advanced_sites=frozenset(),
    )
    source = "def run() -> object:\n    return HandlerPlanted()\n"

    def codes(relative: str) -> list[str]:
        issues = check_contracts_and_di(Path(relative), source, rules, facts)
        return [issue.code for issue in issues]

    assert codes("src/omnimarket/nodes/node_planted/__main__.py") == [_RULE]
    assert (
        codes("src/omnimarket/nodes/node_planted/container/container_planted.py") == []
    )


@pytest.mark.unit
@pytest.mark.parametrize(("node", "factory", "class_name"), _ARGUMENTLESS_FACTORIES)
def test_factory_builds_the_handler(node: str, factory: str, class_name: str) -> None:
    module = importlib.import_module(_container_module(node))
    build: Callable[[], object] = getattr(module, factory)
    assert type(build()).__name__ == class_name


@pytest.mark.unit
def test_version_skew_factory_binds_the_injected_bus_over_the_in_memory_one() -> None:
    module = importlib.import_module(_container_module("node_version_skew_detector"))

    class _Bus:
        async def publish(self, *args: object, **kwargs: object) -> None:
            return None

    bus = _Bus()
    detector = module.create_node_version_skew_detector(event_bus=bus)
    assert detector._event_bus is bus
    info = module.create_node_version_info(name="node_x", version="1.2.3")
    assert (info.name, info.version) == ("node_x", "1.2.3")


class _RecordingBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object]]] = []

    async def publish(self, *, topic: str, key: bytes, value: bytes) -> None:
        self.published.append((topic, json.loads(value)))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("plugin_version", "status", "topic"),
    [
        ("1.2.3", "healthy", "onex.evt.omniclaude.version-skew-healthy.v1"),
        ("9.0.0", "skew_detected", "onex.evt.omniclaude.version-skew-detected.v1"),
    ],
)
def test_version_skew_node_emits_its_outputs_through_the_injected_bus(
    plugin_version: str, status: str, topic: str
) -> None:
    module = importlib.import_module(_container_module("node_version_skew_detector"))
    handler_module = importlib.import_module(
        "omnimarket.nodes.node_version_skew_detector.handlers.handler_version_skew_detector"
    )
    bus = _RecordingBus()
    detector = module.create_node_version_skew_detector(event_bus=bus)
    request = handler_module.VersionSkewCheckRequest(
        plugin_version=plugin_version, runtime_version="1.5.0", installed_nodes=[]
    )
    result = detector.handle(request)
    assert result.status == status
    asyncio.run(detector.emit_skew_event("corr-1"))
    [(published_topic, payload)] = bus.published
    assert published_topic == topic
    assert set(payload) == {
        "status",
        "plugin_version",
        "runtime_version",
        "incompatible_nodes",
        "detected_at",
    }
    assert payload["plugin_version"] == plugin_version
    assert payload["runtime_version"] == "1.5.0"
    assert payload["status"] == status


@pytest.mark.unit
def test_wave_scheduler_factory_binds_the_state_dir_and_run(tmp_path: Path) -> None:
    module = importlib.import_module(
        _container_module("node_wave_scheduler_orchestrator")
    )
    store = module.create_handler_wave_dispatch_state_store(
        state_dir=tmp_path, run_id="run-1"
    )
    assert type(store).__name__ == "HandlerWaveDispatchStateStore"


@pytest.mark.unit
def test_every_slice_node_container_has_an_init() -> None:
    missing = [
        node
        for node in _SLICE_NODES
        if not (_NODES_ROOT / node / "container" / "__init__.py").is_file()
    ]
    assert missing == []
