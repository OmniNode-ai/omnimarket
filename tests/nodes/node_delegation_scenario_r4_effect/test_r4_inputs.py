# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 of OMN-23173: the committed R4 ladder file passes its own schema test."""

from __future__ import annotations

import pytest
import yaml

from omnimarket.enums.enum_delegation_end_of_ladder_reason import (
    EnumDelegationEndOfLadderReason,
)
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.handlers.delegation_readiness_probe import ScriptedLegTransport
from omnimarket.models.delegation.wire.model_probe_credential_ladder import (
    ModelProbeCredentialLadder,
    ModelProbeCredentialRung,
)
from omnimarket.models.delegation.wire.model_probe_ladder import (
    ModelProbeLadder,
    ModelProbeRung,
)
from omnimarket.nodes.node_delegation_scenario_r4_effect.handlers.handler_scenario_r4 import (
    CONTRACT_PATH,
    NODE_DIR,
    ModelR4Inputs,
    load_inputs,
    load_runtime,
)

pytestmark = pytest.mark.unit

INPUTS = NODE_DIR / "inputs" / "r4_legs.yaml"


def test_r4_inputs_parse_into_the_typed_model() -> None:
    inputs = load_inputs(INPUTS)
    assert [leg.name for leg in inputs.recover_legs] == [
        "stopped_then_healthy",
        "killed_then_healthy",
        "slow_then_healthy",
    ]
    assert inputs.all_down_leg.name == "every_rung_down"
    assert inputs.credential_absent_leg.name == "slow_then_absent_then_healthy"


def test_r4_inputs_reject_an_unknown_key() -> None:
    raw = yaml.safe_load(INPUTS.read_text(encoding="utf-8"))
    raw["surprise"] = 1
    with pytest.raises(ValueError, match="surprise"):
        ModelR4Inputs.model_validate(raw)


def test_r4_every_leg_ladder_is_a_valid_probe_ladder() -> None:
    """Each leg's rungs build a real probe ladder (loopback endpoints, distinct backend ids)."""
    inputs = load_inputs(INPUTS)
    legs = [inputs.leg_spec(leg.name, leg.rungs) for leg in inputs.recover_legs]
    legs.append(inputs.leg_spec(inputs.all_down_leg.name, inputs.all_down_leg.rungs))
    legs.append(
        inputs.leg_spec(
            inputs.credential_absent_leg.name, inputs.credential_absent_leg.rungs
        )
    )
    for leg in legs:
        urls = ScriptedLegTransport().endpoints(leg)
        if any(rung.credential_resolved is False for rung in leg.rungs):
            ModelProbeCredentialLadder(
                rungs=tuple(
                    ModelProbeCredentialRung(
                        backend_id=rung.backend_id,
                        model_id=rung.model_id,
                        endpoint_ref=urls[rung.backend_id],
                        timeout_ms=rung.timeout_ms,
                        credential_resolved=rung.credential_resolved,
                    )
                    for rung in leg.rungs
                )
            )
        else:
            ModelProbeLadder(
                rungs=tuple(
                    ModelProbeRung(
                        backend_id=rung.backend_id,
                        model_id=rung.model_id,
                        endpoint_ref=urls[rung.backend_id],
                        timeout_ms=rung.timeout_ms,
                    )
                    for rung in leg.rungs
                )
            )


def test_r4_every_recover_leg_ends_on_a_healthy_rung_and_fails_one_before_it() -> None:
    inputs = load_inputs(INPUTS)
    for leg in inputs.recover_legs:
        assert leg.rungs[-1].behaviour == "answer", leg.name
        assert all(rung.behaviour != "answer" for rung in leg.rungs[:-1]), leg.name
        assert leg.expect_failure_class in {
            c.value for c in EnumDelegationFailureClass
        }, leg.name
        assert leg.expect_failure_class != "unknown", leg.name


def test_r4_slow_rung_waits_longer_than_its_own_deadline() -> None:
    slow = next(
        leg
        for leg in load_inputs(INPUTS).recover_legs
        if leg.name == "slow_then_healthy"
    )
    assert slow.rungs[0].delay_ms > slow.rungs[0].timeout_ms


def test_r4_answer_carries_the_marker_and_reasons_are_the_closed_list() -> None:
    inputs = load_inputs(INPUTS)
    assert inputs.answer_marker in inputs.answer
    assert set(inputs.end_of_ladder_reasons) == {
        r.value for r in EnumDelegationEndOfLadderReason
    }


def test_r4_contract_runtime_block_is_typed_and_names_the_inputs_file() -> None:
    runtime = load_runtime(CONTRACT_PATH)
    assert runtime.scenario_id == "R4"
    assert runtime.cadence.value == "every_6h"
    assert (NODE_DIR / runtime.inputs_relpath) == INPUTS


def test_r4_credential_absent_leg_has_an_absent_rung_before_a_healthy_last_rung() -> (
    None
):
    inputs = load_inputs(INPUTS)
    leg = inputs.credential_absent_leg
    assert any(rung.credential_resolved is False for rung in leg.rungs)
    assert leg.rungs[-1].credential_resolved is True
    assert leg.rungs[-1].behaviour == "answer"


def test_r4_inputs_refuse_a_credential_absent_leg_with_no_absent_rung() -> None:
    raw = yaml.safe_load(INPUTS.read_text(encoding="utf-8"))
    raw["credential_absent_leg"]["rungs"][1]["credential_resolved"] = True
    with pytest.raises(ValueError, match="declares no rung with an absent credential"):
        ModelR4Inputs.model_validate(raw)
