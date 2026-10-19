# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dead-man's two contracts, golden chain and self-test (OMN-20807)."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

from omnimarket.models.liveness.model_automation_liveness import EnumWatchdogSelftest
from omnimarket.models.liveness.model_automation_liveness_deadman import (
    ModelAutomationLivenessDeadmanRequest,
)
from omnimarket.nodes.node_automation_liveness_deadman_compute.handlers import (
    handler_automation_liveness_deadman as compute_module,
)
from omnimarket.nodes.node_automation_liveness_deadman_effect.handlers.handler_automation_liveness_deadman_effect import (
    HandlerAutomationLivenessDeadmanEffect,
    run_selftest,
)
from omnimarket.nodes.node_automation_liveness_deadman_effect.models.model_deadman_record import (
    ModelDeadmanRecordRequest,
)
from tests.nodes.node_automation_liveness_deadman_effect.support import (
    T0,
    raised,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
COMPUTE = NODES / "node_automation_liveness_deadman_compute"
EFFECT = NODES / "node_automation_liveness_deadman_effect"


@pytest.mark.parametrize("node", [COMPUTE, EFFECT], ids=lambda p: p.name)
def test_deadman_contract_routes_its_handler_and_models(node: Path) -> None:
    contract = yaml.safe_load((node / "contract.yaml").read_text("utf-8"))
    handler = contract["handler"]
    module = importlib.import_module(handler["module"])
    assert callable(getattr(module, handler["class"]).handle)
    for key in ("input_model", "output_model"):
        model_path, _, model_name = contract[key].rpartition(".")
        assert getattr(importlib.import_module(model_path), model_name)
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [contract["runtime_dispatch"]["command_topic"]]
    assert contract["terminal_event"] in bus["publish_topics"]
    sources = "\n".join(p.read_text("utf-8") for p in (node / "handlers").glob("*.py"))
    assert "onex.evt." not in sources
    assert "onex.cmd." not in sources


def test_deadman_contracts_name_their_terminal_events_and_command_topics() -> None:
    compute = yaml.safe_load((COMPUTE / "contract.yaml").read_text("utf-8"))
    effect = yaml.safe_load((EFFECT / "contract.yaml").read_text("utf-8"))
    assert (
        compute["terminal_event"]
        == "onex.evt.omnimarket.automation-liveness-deadman-evaluated.v1"
    )
    assert (
        compute["runtime_dispatch"]["command_topic"]
        == "onex.cmd.omnimarket.automation-liveness-deadman-evaluate-requested.v1"
    )
    assert (
        effect["terminal_event"]
        == "onex.evt.omnimarket.automation-liveness-deadman-recorded.v1"
    )
    assert (
        effect["runtime_dispatch"]["command_topic"]
        == "onex.cmd.omnimarket.automation-liveness-deadman-record-requested.v1"
    )


def test_deadman_contract_purity_matches_node_type() -> None:
    compute = yaml.safe_load((COMPUTE / "contract.yaml").read_text("utf-8"))
    effect = yaml.safe_load((EFFECT / "contract.yaml").read_text("utf-8"))
    assert (compute["node_type"], compute["descriptor"]["purity"]) == (
        "compute",
        "pure",
    )
    assert (effect["node_type"], effect["descriptor"]["purity"]) == ("effect", "impure")
    assert (
        "onex.evt.omnimarket.automation-liveness-alarm-recorded.v1"
        in effect["event_bus"]["publish_topics"]
    )
    assert (
        "onex.evt.omnimarket.automation-heartbeat.v1"
        in effect["event_bus"]["publish_topics"]
    )


def test_deadman_effect_builds_without_arguments_and_fails_loudly_unwired(
    tmp_path: Path,
) -> None:
    assert HandlerAutomationLivenessDeadmanEffect()
    with pytest.raises(ValueError, match="no recorded store"):
        HandlerAutomationLivenessDeadmanEffect().handle(
            ModelDeadmanRecordRequest(now=T0, host="host-d", process_started_at=T0)
        )
    # Rows with no ledger wired are a reported failure and a backlog, never a skip.
    from omnimarket.nodes.node_automation_liveness_deadman_compute.handlers.handler_automation_liveness_deadman import (
        HandlerAutomationLivenessDeadman,
    )
    from omnimarket.nodes.node_automation_liveness_deadman_effect.handlers.recorded_store import (
        RecordedStore,
    )

    rows = (
        HandlerAutomationLivenessDeadman()
        .handle(
            ModelAutomationLivenessDeadmanRequest(
                now=T0,
                primary_host="host-p",
                canary_host="host-p",
                primary_last_heartbeat_at=T0,
                opened=(raised("lab-fill", T0),),
            )
        )
        .ledger_rows
    )
    out = HandlerAutomationLivenessDeadmanEffect(
        store=RecordedStore(tmp_path / "d.sqlite")
    ).handle(
        ModelDeadmanRecordRequest(
            now=T0, host="host-d", process_started_at=T0, rows=rows
        )
    )
    assert out.failure == "RuntimeError: no ledger appender is wired"
    assert len(out.backlog) == 1


def test_deadman_selftest_passes_and_fails_when_the_compute_goes_blind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_selftest() is EnumWatchdogSelftest.PASS
    monkeypatch.setattr(
        compute_module.HandlerAutomationLivenessDeadman,
        "_primary_silent_since",
        staticmethod(lambda _request: None),
    )
    assert run_selftest() is EnumWatchdogSelftest.FAIL
