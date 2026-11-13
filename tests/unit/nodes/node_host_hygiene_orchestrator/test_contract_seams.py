# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams of the host hygiene orchestrator (OMN-23707): each fails when either side changes shape.

S1 watermark node -> orchestrator: the host disk read and read-failed topics and their models.
S2 orchestrator -> lab-work serve: the command and terminal topics, the unit model, the executable allow list, every receipt status.
S3 orchestrator -> the cleanup effects: the module and flags each cleanup's argv names are accepted by the effect's own entry point.
S4 orchestrator -> schedule verdict: the schedule run topic the reducer consumes.
S5 orchestrator -> decision compute: the shared decision models.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.models.model_host_disk_reading import (
    ModelHostDiskRead,
    ModelHostDiskReadFailed,
)
from omnimarket.models.model_host_hygiene import (
    ModelHostHygieneDecision,
    ModelHostHygieneDecisionRequest,
)
from omnimarket.models.model_host_hygiene_orchestrator import (
    EnumHostHygieneUnitOutcome,
    ModelHostHygieneOrchestratorOverlay,
)
from omnimarket.models.model_lab_work_unit import (
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.dispatch_host_hygiene import (
    DispatchHostHygieneUnits,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.sender_in_memory import (
    InMemoryHostHygieneSender,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[4] / "src" / "omnimarket" / "nodes"
NODE = "node_host_hygiene_orchestrator"


def _contract(node: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (NODES / node / "contract.yaml").read_text()
    )
    return loaded


ORCHESTRATOR = _contract(NODE)
ROUTES = {r["operation"]: r for r in ORCHESTRATOR["handler_routing"]["handlers"]}
OVERLAY = ModelHostHygieneOrchestratorOverlay.model_validate(
    yaml.safe_load((NODES / NODE / "overlays" / "in_memory.yaml").read_text())
)


def _acl_names(operation: str) -> set[str]:
    rows = ORCHESTRATOR["broker_acl_grants"][0]["grants"]
    return {
        r["name"]
        for r in rows
        if r["resource_type"] == "TOPIC" and r["operation"] == operation
    }


def test_s1_the_read_topics_are_the_ones_the_watermark_node_publishes() -> None:
    published = set(
        _contract("node_lab_disk_watermark_effect")["event_bus"]["publish_topics"]
    )
    assert ROUTES["record_host_disk_read"]["topic"] in published
    assert ROUTES["record_host_disk_read_failed"]["topic"] in published


def test_s1_the_route_models_are_the_models_the_watermark_handler_publishes() -> None:
    watermark = importlib.import_module(
        "omnimarket.nodes.node_lab_disk_watermark_effect.handlers.handler_lab_disk_watermark"
    )
    assert watermark.ModelHostDiskRead is ModelHostDiskRead
    assert watermark.ModelHostDiskReadFailed is ModelHostDiskReadFailed
    for operation, model in (
        ("record_host_disk_read", ModelHostDiskRead),
        ("record_host_disk_read_failed", ModelHostDiskReadFailed),
    ):
        spec = ROUTES[operation]["event_model"]
        assert getattr(importlib.import_module(spec["module"]), spec["name"]) is model


def test_s2_the_acl_rows_name_the_topics_of_the_lab_work_serve() -> None:
    serve = _contract("node_lab_work_unit_effect")
    command = serve["runtime_dispatch"]["command_topic"]
    terminals = set(serve["runtime_dispatch"]["terminal_events"].values())
    assert command in _acl_names("WRITE")
    assert terminals <= _acl_names("READ")
    assert serve["capacity"]["capacity_topic"] in _acl_names("READ")


def test_s2_a_built_unit_is_a_valid_lab_work_unit_with_an_allowed_executable() -> None:
    sender = InMemoryHostHygieneSender()
    from omnimarket.models.model_host_hygiene import (
        EnumHostHygieneReason,
        ModelHostHygieneDecisionItem,
    )

    items = tuple(
        ModelHostHygieneDecisionItem(
            host=OVERLAY.hosts[0].name,
            cleanup_id=cleanup_id,
            due=True,
            reason=EnumHostHygieneReason.DISK_PRESSURE,
        )
        for cleanup_id in OVERLAY.hosts[0].cleanup_ids
    )
    DispatchHostHygieneUnits(OVERLAY, sender).dispatch(items)

    from omnimarket.nodes.node_lab_work_unit_effect.handlers.handler_lab_work_unit_effect import (
        DEFAULT_ALLOWED_EXECUTABLES,
    )

    assert len(sender.sent) == len(items)
    for unit in sender.sent:
        assert ModelLabWorkUnitRequest.model_validate(unit.model_dump())
        assert Path(unit.argv[0]).name in DEFAULT_ALLOWED_EXECUTABLES
        assert unit.repo == OVERLAY.lab_work.repo
        assert unit.commit_sha == OVERLAY.lab_work.commit_sha


EXPECTED_OUTCOME = {
    EnumLabWorkUnitStatus.COMPLETED: EnumHostHygieneUnitOutcome.COMPLETED,
    EnumLabWorkUnitStatus.TIMED_OUT: EnumHostHygieneUnitOutcome.TIMED_OUT,
    EnumLabWorkUnitStatus.REFUSED: EnumHostHygieneUnitOutcome.REFUSED,
    EnumLabWorkUnitStatus.INFRA_ERROR: EnumHostHygieneUnitOutcome.TRANSPORT_UNAVAILABLE,
}


@pytest.mark.parametrize("status", list(EnumLabWorkUnitStatus))
def test_s2_every_receipt_status_of_the_serve_maps_to_a_typed_outcome(
    status: EnumLabWorkUnitStatus,
) -> None:
    from omnimarket.models.model_host_hygiene import (
        EnumHostHygieneReason,
        ModelHostHygieneDecisionItem,
    )

    sender = InMemoryHostHygieneSender({OVERLAY.hosts[0].name: (status, 0, "")})
    item = ModelHostHygieneDecisionItem(
        host=OVERLAY.hosts[0].name,
        cleanup_id=OVERLAY.hosts[0].cleanup_ids[0],
        due=True,
        reason=EnumHostHygieneReason.DISK_PRESSURE,
    )
    (result,) = DispatchHostHygieneUnits(OVERLAY, sender).dispatch((item,))
    assert result.outcome is EXPECTED_OUTCOME[status]


def _module_and_flags(argv: tuple[str, ...]) -> tuple[str, list[str]]:
    index = argv.index("-m")
    return argv[index + 1], [w for w in argv[index + 2 :] if w.startswith("--")]


def test_s3_each_cleanups_module_exists_and_the_hygiene_entry_point_accepts_its_flags(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for cleanup in OVERLAY.cleanups:
        module, flags = _module_and_flags(cleanup.argv)
        assert importlib.util.find_spec(f"{module}.__main__") is not None, module
        if module.endswith("node_lab_disk_hygiene_effect"):
            entry = importlib.import_module(f"{module}.runtime_lab_disk_hygiene_tick")
            words = list(cleanup.argv[cleanup.argv.index(module) + 1 :])
            words = [
                OVERLAY.hosts[0].registry_root if w == "{registry_root}" else w
                for w in words
            ]
            assert entry.main([*words, "--probe"]) == 0
            assert "probe_ok" in capsys.readouterr().out
        if module.endswith("node_retention_cleanup"):
            main = importlib.import_module(f"{module}.__main__").main
            monkeypatch.setattr(sys, "argv", ["prog", *flags, "--help"])
            with pytest.raises(SystemExit) as stop:
                main()
            assert stop.value.code == 0


def test_s4_the_schedule_run_topic_is_the_one_the_schedule_reducer_consumes() -> None:
    reducer = _contract("node_schedule_run_reducer")
    published = ORCHESTRATOR["event_bus"]["publish_topics"]
    assert any(topic in reducer["event_bus"]["subscribe_topics"] for topic in published)
    assert (
        ScheduleGate(NODE, declaration=OVERLAY).declaration().job_id == OVERLAY.job_id
    )


def test_s5_the_decision_compute_contract_names_the_models_the_orchestrator_passes() -> (
    None
):
    compute = _contract("node_host_hygiene_decision_compute")
    for key, model in (
        ("input_model", ModelHostHygieneDecisionRequest),
        ("output_model", ModelHostHygieneDecision),
    ):
        spec = compute[key]
        assert getattr(importlib.import_module(spec["module"]), spec["name"]) is model
