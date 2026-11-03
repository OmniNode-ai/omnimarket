# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor has one definition of integration state, built and done: dev's integrating state (OMN-22106).

A branch that carries its own second declaration (a competing integration state, a second set of verdicts, a second
running-on-dev input) or drops the integrating state turns this red.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml

from omnimarket.models.model_factory_floor_input import (
    ItemState,
    ModelFactoryFloorConfig,
    ModelFactoryFloorRequest,
    ModelFloorLedgerRow,
    ModelFloorManifest,
    ModelFloorPlanInput,
)
from omnimarket.models.model_merged_running_count import ModelMrcEvidence
from omnimarket.nodes.node_factory_floor_compute.handlers import handler_factory_floor
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from tests.nodes.node_factory_floor_compute.support import (
    ago,
    merge,
    pr,
    request,
    row,
    task,
)

pytestmark = pytest.mark.unit

CONTRACT = Path(handler_factory_floor.__file__).resolve().parents[1] / "contract.yaml"
PR = "OmniNode-ai/omnimarket#9001"
PLAN = "beta/plans/2026-11-02-single-integration-plan.md"


def _a1_state(**facts: Any) -> str:
    plan = ModelFloorPlanInput(
        plan_path=PLAN,
        title="Single integration plan",
        manifest=ModelFloorManifest(
            tasks=(task("A1", "0", tickets=("OMN-500",), prs=(PR,)),)
        ),
    )
    base: dict[str, Any] = {
        "plans": (plan,),
        "prs": (pr(PR, "MERGED"),),
        "merges": (merge(PR, 30),),
    }
    result = compute_factory_floor(request(**{**base, **facts}))
    return next(
        i for s in result.lines[0].stations for i in s.items if i.id == "A1"
    ).state


def test_item_state_keeps_the_integrating_state_once() -> None:
    states = get_args(ItemState)
    assert states.count("integrating") == 1
    assert "done" in states


def test_config_declares_exactly_one_integration_state_and_its_verdicts() -> None:
    declared = {
        name for name in ModelFactoryFloorConfig.model_fields if "integrat" in name
    }
    assert declared == {
        "integration_state",
        "integration_pass_verdicts",
        "integration_fail_verdicts",
    }


def test_request_takes_running_on_dev_from_one_input() -> None:
    declared = {
        name
        for name in ModelFactoryFloorRequest.model_fields
        if "running" in name or "integrat" in name
    }
    assert declared == {"running_evidence"}


def test_contract_config_matches_the_model_with_one_integration_state() -> None:
    config = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["config"]
    in_contract = {key for key in config if "integrat" in key}
    assert in_contract == {
        "integration_state",
        "integration_pass_verdicts",
        "integration_fail_verdicts",
    }
    assert config["integration_state"] == "integration"
    assert (
        len(
            re.findall(
                r"^  integration_state:", CONTRACT.read_text(encoding="utf-8"), re.M
            )
        )
        == 1
    )


def test_handler_has_one_proof_and_one_settle_rule() -> None:
    source = Path(handler_factory_floor.__file__).read_text(encoding="utf-8")
    assert len(re.findall(r"^def _proof\(", source, re.M)) == 1
    assert len(re.findall(r"^def _settle\(", source, re.M)) == 1
    assert source.count('"integrating"') >= 1


def test_handler_maps_a_merged_item_without_running_evidence_to_the_dev_state_name() -> (
    None
):
    state = _a1_state(running_evidence=())
    assert state == "integrating"
    assert state in get_args(ItemState)


def test_handler_settles_a_proven_running_merge_to_done() -> None:
    running = ModelMrcEvidence(
        repo="omnimarket",
        kind="deploy-receipt",
        source="deploy agent receipt",
        number=9001,
        observed_at=ago(5),
    )
    proof: ModelFloorLedgerRow = row(
        "STATUS", 4, state="integration", verdict="pass", prs=(PR,)
    )
    assert _a1_state(running_evidence=(running,), ledger=(proof,)) == "done"
