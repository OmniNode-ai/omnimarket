# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26566: each check has its own stored key name; a run for one check never touches the name another check reads.

C11 reads the default name (``PROBE_READS_KEY_NAME``). C16's provision names its own check (``c16``), which the contract maps
to its own generated key id (declared by the secret store node) and its own key row name. The in-memory secret store records
every read and write by (folder, name), so each test proves which names a run touched and that no value already stored changed.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_stability_gateway_key_provision_effect import (
    runtime_stability_gateway_key_provision as runtime,
)
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
    PROBE_READS_KEY_NAME,
    FakeIdentityStore,
    FakeSecretStore,
)

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_stability_gateway_key_provision_effect"
)
C11_NAME = (FOLDER, PROBE_READS_KEY_NAME)
C11_VALUE = "onxk_value-c11-reads-and-nobody-may-change"
C16_NAME = (FOLDER, "STABILITY_TEST_C16_GATEWAY_API_KEY")
C16_VALUE = "onxk_value-already-under-the-c16-name"
DEFAULT_ROW_NAME = "stability-canary-chain-gateway"
C16_ROW_NAME = "stability-canary-c16-gateway"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _handler(
    identity: FakeIdentityStore,
    store: FakeSecretStore,
    contract_path: Path | None = None,
) -> HandlerStabilityGatewayKeyProvision:
    return HandlerStabilityGatewayKeyProvision(
        overlay_lane="stability",
        identity_store=identity,
        settings_store=store.reader,
        key_generator=store.generator,
        contract_path=contract_path or NODE / "contract.yaml",
    )


def _run(
    handler: HandlerStabilityGatewayKeyProvision, check: str | None = None
) -> ModelStabilityKeyProvisionResult:
    return asyncio.run(
        handler.handle(ModelStabilityKeyProvisionCommand(lane="stability", check=check))
    )


# (a) the default name is unchanged for existing callers


def test_default_run_still_stores_under_the_name_c11_reads() -> None:
    store = FakeSecretStore()
    identity = FakeIdentityStore()
    result = _run(_handler(identity, store))
    assert result.outcome is EnumStabilityKeyOutcome.PROVISIONED
    assert store.generator.calls == ["stability_gateway_api_key"]
    assert result.secret_folder == FOLDER
    assert result.secret_key == PROBE_READS_KEY_NAME
    assert result.key_name == DEFAULT_ROW_NAME
    assert store.writes == [C11_NAME]


def test_command_without_a_check_is_the_default_command() -> None:
    assert ModelStabilityKeyProvisionCommand(lane="stability").check is None


def test_default_key_id_and_row_name_in_the_contract_are_unchanged() -> None:
    block = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))[
        "stability_only"
    ]
    assert block["generated_key_id"] == "stability_gateway_api_key"
    assert block["api_key_name"] == DEFAULT_ROW_NAME


# (b) C16's provision never touches the name C11 reads


def test_c16_run_stores_under_its_own_name_and_never_touches_c11s() -> None:
    store = FakeSecretStore()
    identity = FakeIdentityStore()
    result = _run(_handler(identity, store), "c16")
    assert result.outcome is EnumStabilityKeyOutcome.PROVISIONED
    assert store.generator.calls == ["stability_gateway_api_key_c16"]
    assert (result.secret_folder, result.secret_key) == C16_NAME
    assert result.key_name == C16_ROW_NAME
    assert store.writes == [C16_NAME]
    assert C11_NAME not in store.touched()
    assert [name for (_tenant, name) in identity.keys] == [C16_ROW_NAME]


def test_the_c16_key_id_is_declared_by_the_secret_store_with_its_own_name() -> None:
    block = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))[
        "stability_only"
    ]
    key_id = block["checks"]["c16"]["generated_key_id"]
    declared = FakeSecretStore().declared[key_id]
    assert (declared["folder"], declared["key"]) == C16_NAME
    assert declared["prefix"] == "onxk_"
    assert declared["bytes"] >= 32
    assert (declared["folder"], declared["key"]) != C11_NAME
    assert block["checks"]["c16"]["api_key_name"] != block["api_key_name"]


def test_no_two_generated_key_ids_share_a_stored_name() -> None:
    declared = FakeSecretStore().declared
    names = [(spec["folder"], spec["key"]) for spec in declared.values()]
    assert len(names) == len(set(names))


# (c) an existing key is never changed, rotated or revoked


def test_a_key_already_under_the_c16_name_is_left_as_it_is() -> None:
    store = FakeSecretStore({C16_NAME: C16_VALUE, C11_NAME: C11_VALUE})
    identity = FakeIdentityStore()
    result = _run(_handler(identity, store), "c16")
    assert result.outcome is EnumStabilityKeyOutcome.PROVISIONED
    assert store.writes == []
    assert store.values[C16_NAME] == C16_VALUE
    assert store.values[C11_NAME] == C11_VALUE
    assert C11_NAME not in store.touched()
    assert list(identity.keys.values()) == [_sha(C16_VALUE)]


def test_a_c16_run_never_writes_under_c11s_name_and_leaves_c11s_row_alone() -> None:
    store = FakeSecretStore({C11_NAME: C11_VALUE})
    identity = FakeIdentityStore()
    _run(_handler(identity, store))  # C11's key row, as the default provision made it
    rows_before = dict(identity.keys)
    _run(_handler(identity, store), "c16")
    assert C11_NAME not in set(store.writes)
    assert store.values[C11_NAME] == C11_VALUE
    # C11's key row is still there, unchanged and still active; C16 added exactly one more row.
    assert all(identity.keys[key] == value for key, value in rows_before.items())
    assert len(identity.keys) == len(rows_before) + 1
    assert _sha(C11_VALUE) in identity.keys.values()


def test_a_second_c16_run_writes_nothing_new() -> None:
    store = FakeSecretStore()
    identity = FakeIdentityStore()
    _run(_handler(identity, store), "c16")
    writes, rows = list(store.writes), dict(identity.keys)
    again = _run(_handler(identity, store), "c16")
    assert again.outcome is EnumStabilityKeyOutcome.ALREADY_PROVISIONED
    assert store.writes == writes
    assert identity.keys == rows


# a check the contract does not declare, or one that would reuse the default name, is refused with no side effect


def test_a_check_the_contract_does_not_declare_is_refused_with_zero_side_effects() -> (
    None
):
    store = FakeSecretStore({C11_NAME: C11_VALUE})
    identity = FakeIdentityStore()
    result = _run(_handler(identity, store), "c99")
    assert result.ok is False
    assert result.outcome is EnumStabilityKeyOutcome.REFUSED_CHECK_NOT_DECLARED
    assert "c99" in result.detail
    assert store.generator.calls == []
    assert store.touched() == set()
    assert identity.calls == []


def test_a_check_mapped_to_the_default_key_id_is_refused_with_zero_side_effects(
    tmp_path: Path,
) -> None:
    document = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    default_id = document["stability_only"]["generated_key_id"]
    document["stability_only"]["checks"]["c16"]["generated_key_id"] = default_id
    contract = tmp_path / "contract.yaml"
    contract.write_text(yaml.safe_dump(document), encoding="utf-8")
    store = FakeSecretStore({C11_NAME: C11_VALUE})
    identity = FakeIdentityStore()
    result = _run(_handler(identity, store, contract), "c16")
    assert result.outcome is EnumStabilityKeyOutcome.REFUSED_CHECK_NOT_DECLARED
    assert store.generator.calls == []
    assert store.touched() == set()
    assert identity.writes == 0


def test_a_check_mapped_to_the_default_row_name_is_refused_with_zero_side_effects(
    tmp_path: Path,
) -> None:
    document = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    document["stability_only"]["checks"]["c16"]["api_key_name"] = document[
        "stability_only"
    ]["api_key_name"]
    contract = tmp_path / "contract.yaml"
    contract.write_text(yaml.safe_dump(document), encoding="utf-8")
    store = FakeSecretStore()
    identity = FakeIdentityStore()
    result = _run(_handler(identity, store, contract), "c16")
    assert result.outcome is EnumStabilityKeyOutcome.REFUSED_CHECK_NOT_DECLARED
    assert store.touched() == set()
    assert identity.writes == 0


def test_the_lane_guard_still_comes_first_for_a_check() -> None:
    store = FakeSecretStore()
    identity = FakeIdentityStore()
    result = asyncio.run(
        _handler(identity, store).handle(
            ModelStabilityKeyProvisionCommand(lane="prod", check="c16")
        )
    )
    assert result.outcome is EnumStabilityKeyOutcome.REFUSED_LANE_NOT_ALLOWED
    assert store.touched() == set()
    assert identity.calls == []


# the command line carries the check through to the handler


def test_cli_passes_the_check_to_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str | None]] = []

    async def fake_run(lane: str, check: str | None) -> int:
        seen.append((lane, check))
        return 0

    monkeypatch.setattr(runtime, "_run", fake_run)
    assert runtime.main(["run", "--lane", "stability", "--check", "c16"]) == 0
    assert runtime.main(["run", "--lane", "stability"]) == 0
    assert seen == [("stability", "c16"), ("stability", None)]
