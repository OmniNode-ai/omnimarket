# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The mint refuses to run unless the resolved lane is dev, before the key is read and before any secret is written.

Stability, prod and an unset lane each refuse with zero calls to the key reader and the seeder; so does an overlay that
names another lane. The positive control: the same handler with the same fakes mints on the dev lane, so the refusals are
the guard and not a broken fixture. The failing control points the handler at a contract that declares another lane and
shows the dev command is then refused.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dev_proof_tenant_token_effect.handlers import (
    HandlerDevProofTenantToken,
    ModelDevProofTokenCommand,
)
from omnimarket.nodes.node_dev_proof_tenant_token_effect.models.model_dev_proof_tenant_token import (
    EnumDevProofTokenOutcome,
)

from .fakes import FakeKeyReader, FakeSeeder, overlay, rsa_pair

pytestmark = pytest.mark.unit

CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_dev_proof_tenant_token_effect/contract.yaml"
)


def _handler(
    *, lane: str = "dev", contract_path: Path | None = None
) -> tuple[HandlerDevProofTenantToken, FakeKeyReader, FakeSeeder]:
    reader = FakeKeyReader(rsa_pair()[0])
    seeder = FakeSeeder()
    handler = HandlerDevProofTenantToken(
        overlay=overlay(lane=lane),
        key_reader=reader,
        seeder=seeder,
        contract_path=contract_path,
    )
    return handler, reader, seeder


@pytest.mark.parametrize("lane", ["stability", "prod", "staging", "", "DEV"])
def test_dev_proof_token_mint_refuses_non_dev_command_lane(lane: str) -> None:
    handler, reader, seeder = _handler()
    result = asyncio.run(
        handler.handle(ModelDevProofTokenCommand(lane=lane, write=True))
    )
    assert result.outcome is EnumDevProofTokenOutcome.REFUSED_NOT_DEV
    assert result.ok is False
    assert reader.calls == []
    assert seeder.exists_calls == []
    assert seeder.seed_calls == []
    assert seeder.store == {}


def test_dev_proof_token_mint_refuses_non_dev_when_lane_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_LANE", raising=False)
    handler, reader, seeder = _handler()
    result = asyncio.run(handler.handle(ModelDevProofTokenCommand(write=True)))
    assert result.outcome is EnumDevProofTokenOutcome.REFUSED_NOT_DEV
    assert result.lane is None
    assert reader.calls == []
    assert seeder.seed_calls == []


@pytest.mark.parametrize("env_lane", ["stability", "prod"])
def test_dev_proof_token_mint_refuses_non_dev_when_env_lane_is_not_dev(
    monkeypatch: pytest.MonkeyPatch, env_lane: str
) -> None:
    monkeypatch.setenv("ONEX_LANE", env_lane)
    handler, reader, seeder = _handler()
    result = asyncio.run(handler.handle(ModelDevProofTokenCommand(write=True)))
    assert result.outcome is EnumDevProofTokenOutcome.REFUSED_NOT_DEV
    assert reader.calls == []
    assert seeder.seed_calls == []


@pytest.mark.parametrize("overlay_lane", ["stability", "prod"])
def test_dev_proof_token_mint_refuses_non_dev_overlay_lane(overlay_lane: str) -> None:
    handler, reader, seeder = _handler(lane=overlay_lane)
    result = asyncio.run(
        handler.handle(ModelDevProofTokenCommand(lane="dev", write=True))
    )
    assert result.outcome is EnumDevProofTokenOutcome.REFUSED_NOT_DEV
    assert reader.calls == []
    assert seeder.exists_calls == []
    assert seeder.seed_calls == []


def test_dev_proof_token_mint_refuses_non_dev_positive_control_mints_on_dev() -> None:
    handler, reader, seeder = _handler()
    result = asyncio.run(
        handler.handle(ModelDevProofTokenCommand(lane="dev", write=True))
    )
    assert result.outcome is EnumDevProofTokenOutcome.MINTED
    assert len(reader.calls) == 1
    assert len(seeder.seed_calls) == 1


def test_dev_proof_token_mint_refuses_non_dev_reads_the_contract_not_code(
    tmp_path: Path,
) -> None:
    document = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    document["dev_only"]["lane"] = "stability"
    other = tmp_path / "contract.yaml"
    other.write_text(yaml.safe_dump(document), encoding="utf-8")
    handler, reader, seeder = _handler(contract_path=other)
    result = asyncio.run(
        handler.handle(ModelDevProofTokenCommand(lane="dev", write=True))
    )
    assert result.outcome is EnumDevProofTokenOutcome.REFUSED_NOT_DEV
    assert reader.calls == []
    assert seeder.seed_calls == []
