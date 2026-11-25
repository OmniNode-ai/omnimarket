# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 (OMN-26586): the declared path provisions one stability tenant and one active key, and no output holds the key value.

The key is generated inside the secret store node, read once to be hashed and never returned: the result, its JSON, its repr,
the log records and every field of the rows handed to the identity store are searched for the value.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_stability_gateway_key_provision_effect.handlers.handler_stability_gateway_key_provision import (
    HandlerStabilityGatewayKeyProvision,
)
from omnimarket.nodes.node_stability_gateway_key_provision_effect.models.model_stability_gateway_key_provision import (
    EnumStabilityKeyOutcome,
    ModelStabilityKeyProvisionCommand,
    ModelStabilityKeyProvisionResult,
)

from .fakes import (
    FOLDER,
    KEY_NAME,
    KEY_VALUE,
    PROBE_READS_KEY_NAME,
    FakeIdentityStore,
    FakeKeyGenerator,
    FakeSettingStore,
)

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_stability_gateway_key_provision_effect"
)
STORE_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_secret_store_effect/contract.yaml"
)
KEY_HASH = hashlib.sha256(KEY_VALUE.encode("utf-8")).hexdigest()


def _handler(
    identity: FakeIdentityStore,
    generator: FakeKeyGenerator | None = None,
    settings: FakeSettingStore | None = None,
) -> HandlerStabilityGatewayKeyProvision:
    if settings is None:
        settings = FakeSettingStore()
        settings.set(FOLDER, KEY_NAME, KEY_VALUE)
    return HandlerStabilityGatewayKeyProvision(
        overlay_lane="stability",
        identity_store=identity,
        settings_store=settings,
        key_generator=generator or FakeKeyGenerator(),
        contract_path=NODE / "contract.yaml",
    )


def _run(
    handler: HandlerStabilityGatewayKeyProvision,
) -> ModelStabilityKeyProvisionResult:
    return asyncio.run(
        handler.handle(ModelStabilityKeyProvisionCommand(lane="stability"))
    )


def test_stability_run_creates_one_tenant_and_one_active_key_row() -> None:
    identity = FakeIdentityStore()
    result = _run(_handler(identity))
    assert result.ok is True
    assert result.outcome is EnumStabilityKeyOutcome.PROVISIONED
    assert list(identity.tenants) == ["stability-canary"]
    assert len(identity.keys) == 1
    ((tenant_id, name), stored_hash) = next(iter(identity.keys.items()))
    assert tenant_id == identity.tenants["stability-canary"]
    assert name == "stability-canary-chain-gateway"
    # The gateway resolves a presented key by sha256 of the whole value, prefix included.
    assert stored_hash == KEY_HASH


def test_no_output_field_holds_the_key_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    identity = FakeIdentityStore()
    with caplog.at_level(logging.DEBUG):
        result = _run(_handler(identity))
    haystacks = [
        result.model_dump_json(),
        repr(result),
        str(result.model_dump()),
        caplog.text,
        repr(identity.keys.values()),
    ]
    for text in haystacks:
        assert KEY_VALUE not in text
        assert KEY_VALUE.removeprefix("onxk_") not in text
    assert result.secret_folder == FOLDER
    assert result.secret_key == KEY_NAME


def test_result_model_has_no_field_that_can_hold_a_value() -> None:
    names = set(ModelStabilityKeyProvisionResult.model_fields)
    assert names == {
        "ok",
        "outcome",
        "detail",
        "lane",
        "tenant_id",
        "tenant_slug",
        "key_name",
        "secret_folder",
        "secret_key",
        "expires_at",
    }


def test_the_key_is_generated_by_the_declared_id_and_written_nowhere_else() -> None:
    generator = FakeKeyGenerator()
    _run(_handler(FakeIdentityStore(), generator))
    assert generator.calls == ["stability_gateway_api_key"]


def test_second_run_on_the_same_database_is_already_provisioned_and_writes_nothing_new() -> (
    None
):
    identity = FakeIdentityStore()
    handler = _handler(identity, FakeKeyGenerator("created"))
    _run(handler)
    again = _run(_handler(identity, FakeKeyGenerator("exists")))
    assert again.ok is True
    assert again.outcome is EnumStabilityKeyOutcome.ALREADY_PROVISIONED
    assert len(identity.tenants) == 1
    assert len(identity.keys) == 1


def test_emptied_database_with_a_key_already_in_the_store_is_provisioned_again() -> (
    None
):
    emptied = FakeIdentityStore()
    result = _run(_handler(emptied, FakeKeyGenerator("exists")))
    assert result.outcome is EnumStabilityKeyOutcome.PROVISIONED
    assert emptied.keys
    assert emptied.tenants


def test_key_absent_from_the_store_after_generation_writes_nothing() -> None:
    identity = FakeIdentityStore()
    result = _run(_handler(identity, settings=FakeSettingStore()))
    assert result.outcome is EnumStabilityKeyOutcome.KEY_UNAVAILABLE
    assert identity.writes == 0


def test_generator_refusal_writes_nothing() -> None:
    identity = FakeIdentityStore()
    result = _run(_handler(identity, FakeKeyGenerator("not_configured")))
    assert result.outcome is EnumStabilityKeyOutcome.KEY_UNAVAILABLE
    assert identity.writes == 0


def test_tenant_slug_held_by_another_owner_is_refused_with_no_key_row() -> None:
    identity = FakeIdentityStore(slug_taken=True)
    result = _run(_handler(identity))
    assert result.outcome is EnumStabilityKeyOutcome.TENANT_REFUSED
    assert identity.keys == {}


def test_store_unavailable_is_a_typed_outcome_naming_the_class_only() -> None:
    result = _run(_handler(FakeIdentityStore(unavailable=True)))
    assert result.outcome is EnumStabilityKeyOutcome.STORE_UNAVAILABLE
    assert "ConnectionRefusedError" in result.detail


def test_key_hash_held_elsewhere_is_refused() -> None:
    identity = FakeIdentityStore(hashes_held_by_others=frozenset({KEY_HASH}))
    result = _run(_handler(identity))
    assert result.outcome is EnumStabilityKeyOutcome.KEY_HASH_TAKEN


def test_different_key_already_active_under_the_name_is_a_conflict_not_a_replacement() -> (
    None
):
    identity = FakeIdentityStore()
    _run(_handler(identity))
    other = FakeSettingStore()
    other.set(FOLDER, KEY_NAME, "onxk_a-different-value")
    result = _run(_handler(identity, FakeKeyGenerator("exists"), other))
    assert result.outcome is EnumStabilityKeyOutcome.KEY_NAME_CONFLICT
    assert list(identity.keys.values()) == [KEY_HASH]


def test_seam_generated_key_id_is_declared_by_the_secret_store_with_the_gateway_prefix() -> (
    None
):
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    key_id = contract["stability_only"]["generated_key_id"]
    declared = yaml.safe_load(STORE_CONTRACT.read_text(encoding="utf-8"))[
        "generated_keys"
    ][key_id]
    assert declared["prefix"] == "onxk_"
    assert declared["bytes"] >= 32


def test_seam_stored_key_name_is_the_name_the_probes_read() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    key_id = contract["stability_only"]["generated_key_id"]
    declared = yaml.safe_load(STORE_CONTRACT.read_text(encoding="utf-8"))[
        "generated_keys"
    ][key_id]
    assert declared["key"] == PROBE_READS_KEY_NAME
    assert declared["folder"] == FOLDER
