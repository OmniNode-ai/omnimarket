# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 (OMN-26586): prod and the cloud are refused before any secret read or database write.

A command lane the contract's ``stability_only`` block does not declare, and an overlay lane that differs from the command's,
are each ``refused_lane_not_allowed`` with zero calls to the key generator, the settings store and the identity store. The
failing control writes a contract that declares another lane: the same handler then runs the whole flow, so the refusal is the
guard reading the contract, not an accident.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_stability_gateway_key_provision_effect.handlers.handler_stability_gateway_key_provision import (
    HandlerStabilityGatewayKeyProvision,
)
from omnimarket.nodes.node_stability_gateway_key_provision_effect.models.model_stability_gateway_key_provision import (
    EnumStabilityKeyOutcome,
    ModelStabilityKeyProvisionCommand,
)

from .fakes import (
    FOLDER,
    KEY_NAME,
    KEY_VALUE,
    FakeIdentityStore,
    FakeKeyGenerator,
    FakeSettingStore,
)

pytestmark = pytest.mark.unit

CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_stability_gateway_key_provision_effect/contract.yaml"
)


@dataclass
class _Rig:
    handler: HandlerStabilityGatewayKeyProvision
    generator: FakeKeyGenerator
    settings: FakeSettingStore
    identity: FakeIdentityStore


def _build(overlay_lane: str = "stability", contract_path: Path | None = None) -> _Rig:
    generator = FakeKeyGenerator()
    settings = FakeSettingStore()
    settings.set(FOLDER, KEY_NAME, KEY_VALUE)
    identity = FakeIdentityStore()
    handler = HandlerStabilityGatewayKeyProvision(
        overlay_lane=overlay_lane,
        identity_store=identity,
        settings_store=settings,
        key_generator=generator,
        contract_path=contract_path or CONTRACT_PATH,
    )
    return _Rig(handler, generator, settings, identity)


def _run(rig: _Rig, lane: str):  # type: ignore[no-untyped-def]
    return asyncio.run(rig.handler.handle(ModelStabilityKeyProvisionCommand(lane=lane)))


def _assert_zero_side_effects(rig: _Rig) -> None:
    assert rig.generator.calls == []
    assert rig.settings.calls == []
    assert rig.identity.calls == []
    assert rig.identity.writes == 0


@pytest.mark.parametrize("lane", ["prod", "cloud", "dev", "public", ""])
def test_lane_the_contract_does_not_declare_is_refused_with_zero_side_effects(
    lane: str,
) -> None:
    rig = _build()
    result = _run(rig, lane)
    assert result.ok is False
    assert result.outcome is EnumStabilityKeyOutcome.REFUSED_LANE_NOT_ALLOWED
    assert "not" in result.detail
    _assert_zero_side_effects(rig)


@pytest.mark.parametrize("overlay_lane", ["prod", "cloud", "dev"])
def test_overlay_lane_that_is_not_stability_is_refused_even_for_a_stability_command(
    overlay_lane: str,
) -> None:
    rig = _build(overlay_lane=overlay_lane)
    result = _run(rig, "stability")
    assert result.outcome is EnumStabilityKeyOutcome.REFUSED_LANE_NOT_ALLOWED
    assert overlay_lane in result.detail
    _assert_zero_side_effects(rig)


def test_positive_control_stability_runs_the_whole_flow() -> None:
    rig = _build()
    result = _run(rig, "stability")
    assert result.ok is True
    assert rig.generator.calls != []
    assert rig.identity.writes == 2


def test_failing_control_a_contract_declaring_prod_lets_prod_through(
    tmp_path: Path,
) -> None:
    document = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    document["stability_only"]["lanes"] = ["prod"]
    contract = tmp_path / "contract.yaml"
    contract.write_text(yaml.safe_dump(document), encoding="utf-8")
    rig = _build(overlay_lane="prod", contract_path=contract)
    assert _run(rig, "prod").ok is True
    stability = _build(contract_path=contract)
    assert (
        _run(stability, "stability").outcome
        is EnumStabilityKeyOutcome.REFUSED_LANE_NOT_ALLOWED
    )
    _assert_zero_side_effects(stability)


def test_the_contract_declares_stability_and_neither_prod_nor_cloud() -> None:
    declared = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))[
        "stability_only"
    ]["lanes"]
    assert declared == ["stability"]
