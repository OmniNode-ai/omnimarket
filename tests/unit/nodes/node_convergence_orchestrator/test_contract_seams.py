# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams of the convergence orchestrator, each failing when either side changes shape (OMN-24198).

S1 lab-work serve: the command, receipt and capacity topics the orchestrator names are the ones the serve contract declares.
S2 schedule reducer: the orchestrator's schedule-run topic is the one the reducer subscribes to.
S3 host command: the clone statuses the dirty-converge tick prints are statuses the parser reads, and the module the overlay's
   argv names exposes the ``--observe`` option the observe command needs.
S4 publish topics: the two topics the node publishes.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from omnimarket.models.model_convergence_orchestrator import (
    EnumConvergenceCloneStatus,
)
from omnimarket.nodes.node_canonical_clone_dirty_converge_effect.models.model_canonical_clone_dirty_converge import (
    EnumDirtyConvergeStatus,
)
from omnimarket.nodes.node_convergence_orchestrator.handlers.contract_config import (
    run_completed_topic,
    schedule_run_topic,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[4] / "src" / "omnimarket" / "nodes"


def contract(node: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (NODES / node / "contract.yaml").read_text()
    )
    return loaded


ORCH = contract("node_convergence_orchestrator")


def test_s1_the_lab_work_topics_are_the_serve_contracts_topics() -> None:
    serve = contract("node_lab_work_unit_effect")
    seam = ORCH["lab_work_seam"]

    assert seam["command_topic"] == serve["runtime_dispatch"]["command_topic"]
    assert set(seam["receipt_topics"]) == set(
        serve["runtime_dispatch"]["terminal_events"].values()
    )
    assert seam["capacity_topic"] == serve["capacity"]["capacity_topic"]


def test_s1_the_runtime_principal_may_write_the_command_and_read_each_receipt() -> None:
    grants = {
        (g["name"], g["operation"])
        for g in ORCH["broker_acl_grants"][0]["grants"]
        if g["resource_type"] == "TOPIC"
    }
    seam = ORCH["lab_work_seam"]

    assert (seam["command_topic"], "WRITE") in grants
    for topic in (*seam["receipt_topics"], seam["capacity_topic"]):
        assert (topic, "READ") in grants


def test_s2_the_schedule_run_topic_is_the_one_the_reducer_subscribes_to() -> None:
    reducer = contract("node_schedule_run_reducer")

    assert schedule_run_topic() in reducer["event_bus"]["subscribe_topics"]


def test_s3_every_status_the_tick_prints_is_a_status_the_parser_reads() -> None:
    tick = {s.value for s in EnumDirtyConvergeStatus}
    parser = {s.value for s in EnumConvergenceCloneStatus}

    assert tick <= parser


def test_s3_the_overlay_names_a_module_whose_command_has_the_observe_option() -> None:
    overlay = yaml.safe_load(
        (NODES / "node_convergence_orchestrator/overlays/in_memory.yaml").read_text()
    )
    argv: list[str] = overlay["observe_command"]["argv"]
    module = importlib.import_module(argv[argv.index("-m") + 1])

    option_names = {
        name for p in module.dirty_converge_command.params for name in p.opts
    }
    assert argv[-1] in option_names
    assert (
        "--observe"
        in CliRunner().invoke(module.dirty_converge_command, ["--help"]).output
    )


def test_s3_the_act_command_is_the_observe_command_without_its_flag() -> None:
    overlay = yaml.safe_load(
        (NODES / "node_convergence_orchestrator/overlays/in_memory.yaml").read_text()
    )

    assert overlay["observe_command"]["argv"][:-1] == overlay["act_command"]["argv"]


def test_s4_the_node_publishes_the_run_and_the_schedule_run_topics() -> None:
    assert run_completed_topic() == "onex.evt.omnimarket.convergence-run-completed.v1"
    assert schedule_run_topic() == "onex.evt.omnimarket.schedule-run.v1"
    assert ORCH["terminal_event"] == run_completed_topic()
    assert ORCH["event_bus"]["publish_topics"] == [
        run_completed_topic(),
        schedule_run_topic(),
    ]
