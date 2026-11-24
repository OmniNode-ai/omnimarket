# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The signing key the node's own entry resolves from the secret store when the variable is unset (OMN-24611).

* golden: with the variable ``hmac_secret_env`` names unset, the entry signs with the key the overlay's
  ``signing_key_store`` names and publishes once — the failing control drives the same input with an overlay
  without the block and gets the old missing-key refusal, so the green is the store resolution's;
* variable set, verified (OMN-24611 verify_env_key): with the check on, a variable equal to the store key signs
  and the store is asked once for the comparison; a variable that differs from the store key — the stale SHARED
  key a lab host keeps in its environment — is a typed refusal naming both check values with nothing published,
  and the failing control shows the same differing variable publishing under ``verify_env_key: false``, so the
  green is the check's;
* error: an unreachable store, a store that raises, a store without the key and an unconfigured store each
  refuse with the store outcome named, publish nothing, and leave the key in no result, error or log — with the
  variable unset (the resolution path) and with it set (the verification path) alike;
* leak: the reading model's repr, dump and JSON carry no part of the key — the control shows a detail string
  still passes through a dump, so the absence is the key field's exclusion and not a scrubbed dump;
* seam: the example overlay's ``signing_key_store`` validates through the overlay model, a malformed block is
  refused, a block without ``verify_env_key`` (now required) or with a non-bool one is refused, and the
  contract's secret_dependencies still name the variable the overlay's ``hmac_secret_env`` names;
  the real ``build_handler`` wires the store reader from the block (a build that forgets the wiring refuses, so the
  golden's green is not an artifact of the hermetic wiring alone).

The store is a fake of the omnibase_spi ``ProtocolSecretStore`` seam handed to ``HandlerSecretStore``: with a
store injected the handler selects no adapter and touches no network, identity or machine config, so the entry
is driven exactly as ``test_send_from_outside_runtime.py`` drives it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import SecretStr, ValidationError

from omnimarket.models.bus_binding.model_node_broker import ModelNodeBroker
from omnimarket.models.model_deploy_agent_key_check import key_check_value
from omnimarket.models.secret_store.model_secret_store_overlay import (
    ModelSecretStoreOverlay,
)
from omnimarket.models.secret_store.model_secret_store_request import (
    ModelSecretStoreRequest,
)
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
    ModelSecretStoreResult,
)
from omnimarket.nodes.node_branch_deploy_command_effect import __main__ as entry
from omnimarket.nodes.node_branch_deploy_command_effect.adapters.adapter_signing_key_store import (
    SigningKeyStoreReader,
)
from omnimarket.nodes.node_branch_deploy_command_effect.handlers.handler_branch_deploy_command import (
    HandlerBranchDeployCommand,
    load_command_topic,
)
from omnimarket.nodes.node_branch_deploy_command_effect.models.model_signing_key_reading import (
    ModelSigningKeyReading,
)
from omnimarket.nodes.node_branch_deploy_command_effect.overlay import (
    ModelBranchDeployCommandOverlay,
    ModelSigningKeyStoreOverlay,
)
from omnimarket.nodes.node_branch_deploy_command_effect.protocols.protocol_signing_key import (
    ProtocolSigningKey,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)
from tests.nodes.node_branch_deploy_command_effect.support import (
    NODE_DIR,
    RUN_ID,
    SIGNING_ENV,
    SIGNING_KEY,
    FakeBus,
    FakeKeyCheckReader,
    overlay,
    request,
)

pytestmark = pytest.mark.unit

STORE_KEY = "store-fixture-key"  # fixture value, not a credential
ENV_KEY = "env-fixture-key"  # fixture value, not a credential
STORE_PATH = "/shared/env/EXAMPLE_SIGNING_KEY"


class FakeSecretStore:
    """The SPI store seam with one planted value; records every key it is asked for."""

    def __init__(self, value: str | None, error: Exception | None = None) -> None:
        self.value = value
        self.error = error
        self.asked: list[str] = []

    async def get_secret(self, key: str) -> str | None:
        self.asked.append(key)
        if self.error is not None:
            raise self.error
        return self.value

    async def set_secret(self, key: str, value: str) -> bool:
        raise RuntimeError("the fake store takes no writes")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        return []


def _expected_signature(body: dict[str, Any], key: str) -> str:
    """The HMAC the handler must have signed the body under."""
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()


def _hermetic_build(
    bus: FakeBus,
    fake: FakeSecretStore,
    key_check: FakeKeyCheckReader,
    store_overlay: ModelSecretStoreOverlay | None,
    onex_home: Path,
    node_overlay: ModelBranchDeployCommandOverlay | None = None,
) -> Callable[..., HandlerBranchDeployCommand]:
    """The wiring of ``__main__.build_handler`` over the fake bus and store.

    ``store_overlay`` None makes the store unconfigured: the handler then reads
    ``<onex_home>/config.yaml``, which the test keeps empty. ``node_overlay``
    None builds the example overlay; a test passes its own to vary a block the
    example cannot carry.
    """

    def _build(*, environ: Mapping[str, str]) -> HandlerBranchDeployCommand:
        resolved_overlay = node_overlay if node_overlay is not None else overlay()
        signing_key_reader: ProtocolSigningKey | None = None
        if resolved_overlay.signing_key_store is not None:
            store = resolved_overlay.signing_key_store
            signing_key_reader = SigningKeyStoreReader(
                store.folder,
                store.key,
                HandlerSecretStore(
                    store=fake, overlay=store_overlay, onex_home=onex_home
                ),
            )
        return HandlerBranchDeployCommand(
            event_bus=bus,
            overlay=resolved_overlay,
            environ=environ,
            key_check_reader=key_check,
            signing_key_reader=signing_key_reader,
        )

    return _build


def _without_store_block_build(
    bus: FakeBus, key_check: FakeKeyCheckReader
) -> Callable[..., HandlerBranchDeployCommand]:
    """A build_handler for an overlay without ``signing_key_store``: no reader is wired, as the entry does."""

    def _build(*, environ: Mapping[str, str]) -> HandlerBranchDeployCommand:
        return HandlerBranchDeployCommand(
            event_bus=bus,
            overlay=overlay().model_copy(update={"signing_key_store": None}),
            environ=environ,
            key_check_reader=key_check,
            signing_key_reader=None,
        )

    return _build


def test_golden_the_entry_signs_with_the_store_key_and_publishes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The ticket's falsifier: variable unset, the store holds the key -> the
    command is signed under the stored key and published once."""
    monkeypatch.delenv(SIGNING_ENV, raising=False)
    bus = FakeBus()
    fake = FakeSecretStore(STORE_KEY)
    check = FakeKeyCheckReader("read", key_check_value(STORE_KEY))
    store_block = overlay().signing_key_store
    assert store_block is not None
    monkeypatch.setattr(
        entry,
        "build_handler",
        _hermetic_build(bus, fake, check, store_block.secret_store, tmp_path),
    )

    code = entry.main(["--input", request(mode="deploy").model_dump_json()])

    assert code == 0
    out = capsys.readouterr()
    result = json.loads(out.out)
    assert result["status"] == "published"
    assert out.err == ""
    assert len(bus.published) == 1
    topic, key, value = bus.published[0]
    assert topic == load_command_topic()
    assert key == str(RUN_ID).encode()
    payload = json.loads(value)
    body = {k: v for k, v in payload.items() if k != "_signature"}
    assert payload["_signature"] == _expected_signature(body, STORE_KEY)
    assert fake.asked == [STORE_PATH]


def test_failing_control_without_the_store_block_the_entry_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same input with an overlay without ``signing_key_store`` gets the
    existing missing-key refusal, so the golden's green is the change's."""
    monkeypatch.delenv(SIGNING_ENV, raising=False)
    bus = FakeBus()
    fake = FakeSecretStore(STORE_KEY)
    check = FakeKeyCheckReader("read", key_check_value(STORE_KEY))
    monkeypatch.setattr(entry, "build_handler", _without_store_block_build(bus, check))

    code = entry.main(["--input", request(mode="deploy").model_dump_json()])

    assert code == 1
    out = capsys.readouterr()
    result = json.loads(out.out)
    assert result["status"] == "refused"
    assert (
        result["detail"]
        == "the deploy agent rejects unsigned commands and the signing key "
        "named by the overlay is not set"
    )
    assert bus.published == []
    assert fake.asked == []


def test_golden_env_key_equal_to_the_store_key_publishes_and_the_store_is_asked_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """verify_env_key true and the variable equal to the store key: the command
    signs under that key, publishes once, and the store is asked once to check it."""
    monkeypatch.setenv(SIGNING_ENV, STORE_KEY)
    bus = FakeBus()
    fake = FakeSecretStore(STORE_KEY)
    check = FakeKeyCheckReader("read", key_check_value(STORE_KEY))
    store_block = overlay().signing_key_store
    assert store_block is not None
    monkeypatch.setattr(
        entry,
        "build_handler",
        _hermetic_build(bus, fake, check, store_block.secret_store, tmp_path),
    )

    code = entry.main(["--input", request(mode="deploy").model_dump_json()])

    assert code == 0
    out = capsys.readouterr()
    result = json.loads(out.out)
    assert result["status"] == "published"
    assert len(bus.published) == 1
    payload = json.loads(bus.published[0][2])
    body = {k: v for k, v in payload.items() if k != "_signature"}
    assert payload["_signature"] == _expected_signature(body, STORE_KEY)
    assert fake.asked == [STORE_PATH]
    blob = out.out + out.err
    assert STORE_KEY not in blob
    assert ENV_KEY not in blob


def test_failing_control_verify_env_key_false_publishes_the_stale_env_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The same differing variable with ``verify_env_key: false`` publishes signed
    under the environment key and the store is never asked — the old silent
    behaviour the check exists to close."""
    monkeypatch.setenv(SIGNING_ENV, ENV_KEY)
    bus = FakeBus()
    fake = FakeSecretStore(STORE_KEY)
    check = FakeKeyCheckReader("read", key_check_value(ENV_KEY))
    store_block = overlay().signing_key_store
    assert store_block is not None
    unverified = store_block.model_copy(update={"verify_env_key": False})
    node_overlay = overlay().model_copy(update={"signing_key_store": unverified})
    monkeypatch.setattr(
        entry,
        "build_handler",
        _hermetic_build(
            bus, fake, check, store_block.secret_store, tmp_path, node_overlay
        ),
    )

    code = entry.main(["--input", request(mode="deploy").model_dump_json()])

    assert code == 0
    out = capsys.readouterr()
    result = json.loads(out.out)
    assert result["status"] == "published"
    assert len(bus.published) == 1
    payload = json.loads(bus.published[0][2])
    body = {k: v for k, v in payload.items() if k != "_signature"}
    assert payload["_signature"] == _expected_signature(body, ENV_KEY)
    assert fake.asked == []


def test_error_env_key_that_differs_from_the_store_key_refuses(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """A stale environment key under verify_env_key true is a typed refusal:
    nothing published, both one-way check values named in the typed fields, and
    no key text anywhere in the result or the logs."""
    monkeypatch.setenv(SIGNING_ENV, ENV_KEY)
    bus = FakeBus()
    fake = FakeSecretStore(STORE_KEY)
    check = FakeKeyCheckReader("read", key_check_value(STORE_KEY))
    store_block = overlay().signing_key_store
    assert store_block is not None
    monkeypatch.setattr(
        entry,
        "build_handler",
        _hermetic_build(bus, fake, check, store_block.secret_store, tmp_path),
    )

    code = entry.main(["--input", request(mode="deploy").model_dump_json()])

    assert code == 1
    out = capsys.readouterr()
    result = json.loads(out.out)
    assert result["status"] == "refused"
    assert result["detail"] == (
        "the signing key in the environment is not the dev signing key of the "
        f"store (signer check {key_check_value(ENV_KEY)}, store check "
        f"{key_check_value(STORE_KEY)}); nothing was published"
    )
    assert out.err == f"{result['detail']}\n"
    assert bus.published == []
    assert fake.asked == [STORE_PATH]
    assert check.reads == 0
    assert result["signer_key_check"] == key_check_value(ENV_KEY)
    assert result["store_key_check"] == key_check_value(STORE_KEY)
    # The agent was never asked, so no agent check value is named.
    assert result["agent_key_check"] == ""
    blob = out.out + out.err + caplog.text
    assert ENV_KEY not in blob
    assert STORE_KEY not in blob
    assert SIGNING_KEY not in blob


def test_error_mismatch_store_key_a_and_an_agent_serving_the_check_of_key_b_refuses(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Variable unset, the store returns key A, the agent serves the check of key B:
    the store path signs with A, the key check refuses it and nothing is published.
    Every other variable-unset test hands the reader the store key's own check value."""
    key_a = STORE_KEY
    key_b = "other-agent-fixture-key"  # fixture value, not a credential
    assert key_check_value(key_a) != key_check_value(key_b)
    monkeypatch.delenv(SIGNING_ENV, raising=False)
    bus = FakeBus()
    fake = FakeSecretStore(key_a)
    check = FakeKeyCheckReader("read", key_check_value(key_b))
    store_block = overlay().signing_key_store
    assert store_block is not None
    monkeypatch.setattr(
        entry,
        "build_handler",
        _hermetic_build(bus, fake, check, store_block.secret_store, tmp_path),
    )

    code = entry.main(["--input", request(mode="deploy").model_dump_json()])

    assert code == 1
    out = capsys.readouterr()
    result = json.loads(out.out)
    assert result["status"] == "refused"
    assert result["detail"] == (
        "the signing key does not match the deploy agent's key (signer check "
        f"{key_check_value(key_a)}, agent check {key_check_value(key_b)}); "
        "nothing was published"
    )
    assert out.err == f"{result['detail']}\n"
    assert bus.published == []
    assert fake.asked == [STORE_PATH]
    assert check.reads == 1
    assert result["signer_key_check"] == key_check_value(key_a)
    assert result["agent_key_check"] == key_check_value(key_b)
    blob = out.out + out.err + caplog.text
    assert key_a not in blob
    assert key_b not in blob


async def test_the_adapter_turns_a_raising_handler_into_a_refusal_naming_the_class() -> (
    None
):
    class _Raising(HandlerSecretStore):
        async def handle(self, request: Any) -> Any:
            raise ConnectionResetError("secret detail that must not travel")

    reading = await SigningKeyStoreReader(
        "/shared/env", "EXAMPLE_SIGNING_KEY", _Raising()
    ).read()
    assert reading.status == "refused"
    assert reading.detail == "ConnectionResetError"
    assert "secret detail" not in repr(reading)


async def test_the_adapter_names_a_resolved_but_empty_value() -> None:
    class _Empty(HandlerSecretStore):
        async def handle(
            self, request: ModelSecretStoreRequest
        ) -> ModelSecretStoreResult:
            return ModelSecretStoreResult(
                operation=request.operation,
                outcome=EnumSecretStoreOutcome.RESOLVED,
                folder=request.folder,
                key=request.key,
                value=SecretStr("  "),
            )

    reading = await SigningKeyStoreReader(
        "/shared/env", "EXAMPLE_SIGNING_KEY", _Empty()
    ).read()
    assert reading.status == "refused"
    assert reading.detail == "resolved but the stored value is empty"


@pytest.mark.parametrize(
    ("broken", "outcome"),
    [
        ("raising", "failed"),
        ("missing", "not_found"),
    ],
)
def test_error_env_key_set_and_the_store_does_not_return_the_dev_key_refuses(
    broken: str,
    outcome: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """With the variable set and the check on, a store that raises or holds no key
    refuses with the store outcome named and nothing published."""
    monkeypatch.setenv(SIGNING_ENV, ENV_KEY)
    fake = (
        FakeSecretStore(None, error=RuntimeError("exploded"))
        if broken == "raising"
        else FakeSecretStore(None)
    )
    bus = FakeBus()
    check = FakeKeyCheckReader("read", key_check_value(STORE_KEY))
    store_block = overlay().signing_key_store
    assert store_block is not None
    monkeypatch.setattr(
        entry,
        "build_handler",
        _hermetic_build(bus, fake, check, store_block.secret_store, tmp_path),
    )

    code = entry.main(["--input", request(mode="deploy").model_dump_json()])

    assert code == 1
    out = capsys.readouterr()
    result = json.loads(out.out)
    assert result["status"] == "refused"
    assert result["detail"] == (
        "the signing key variable is set but the secret store did not return "
        f"the dev key to check it against ({outcome}); nothing was published"
    )
    assert out.err == f"{result['detail']}\n"
    assert bus.published == []
    assert fake.asked == [STORE_PATH]
    assert result["signer_key_check"] == result["agent_key_check"] == ""
    blob = out.out + out.err + caplog.text
    assert STORE_KEY not in blob
    assert ENV_KEY not in blob
    assert SIGNING_KEY not in blob


@pytest.mark.parametrize(
    ("broken", "outcome", "reached"),
    [
        ("unreachable", "unreachable", True),
        ("raising", "failed", True),
        ("missing", "not_found", True),
        ("unconfigured", "not_configured", False),
    ],
)
def test_error_chain_a_store_that_does_not_return_the_key_refuses(
    broken: str,
    outcome: str,
    reached: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Every way a store fails to return the key is a typed refusal naming the
    store outcome, with nothing published and no key value anywhere."""
    monkeypatch.delenv(SIGNING_ENV, raising=False)
    if broken == "unreachable":
        fake = FakeSecretStore(None, error=ConnectionError("bus down"))
    elif broken == "raising":
        fake = FakeSecretStore(None, error=RuntimeError("exploded"))
    elif broken == "missing":
        fake = FakeSecretStore(None)
    else:
        fake = FakeSecretStore(STORE_KEY)
    bus = FakeBus()
    check = FakeKeyCheckReader("read", key_check_value(STORE_KEY))
    store_block = overlay().signing_key_store
    assert store_block is not None
    store_overlay = None if broken == "unconfigured" else store_block.secret_store
    monkeypatch.setattr(
        entry,
        "build_handler",
        _hermetic_build(
            bus, fake, check, store_overlay, tmp_path / "onex-home-without-config"
        ),
    )

    code = entry.main(["--input", request(mode="deploy").model_dump_json()])

    assert code == 1
    out = capsys.readouterr()
    result = json.loads(out.out)
    assert result["status"] == "refused"
    assert result["detail"] == (
        "the signing key variable is not set and the secret store did not "
        f"return it ({outcome}); nothing was published"
    )
    assert out.err == f"{result['detail']}\n"
    assert bus.published == []
    assert fake.asked == ([STORE_PATH] if reached else [])
    blob = out.out + out.err + caplog.text
    assert STORE_KEY not in blob
    assert ENV_KEY not in blob
    assert SIGNING_KEY not in blob


def test_the_reading_model_cannot_leak_the_key() -> None:
    reading = ModelSigningKeyReading(status="read", detail="", key=SecretStr(STORE_KEY))
    field = ModelSigningKeyReading.model_fields["key"]
    assert field.exclude is True
    assert field.repr is False

    rendered = (
        repr(reading),
        str(reading),
        json.dumps(reading.model_dump()),
        reading.model_dump_json(),
        json.dumps(reading.model_dump(mode="json")),
    )
    for text in rendered:
        assert STORE_KEY not in text
    assert reading.model_dump() == {"status": "read", "detail": ""}
    assert reading.model_dump_json() == '{"status":"read","detail":""}'
    refused = ModelSigningKeyReading(status="refused", detail="not_found")
    assert refused.key.get_secret_value() == ""


def test_failing_control_a_detail_still_passes_through_a_dump() -> None:
    """A plain field keeps its text in a dump, so the leak test above is the
    key field's exclusion and not a dump that strips everything."""
    refused = ModelSigningKeyReading(status="refused", detail=STORE_KEY)
    assert STORE_KEY in refused.model_dump_json()


def test_seam_example_overlay_signing_key_store_validates_through_the_model() -> None:
    store = overlay().signing_key_store

    assert isinstance(store, ModelSigningKeyStoreOverlay)
    assert (store.folder, store.key) == ("/shared/env", "EXAMPLE_SIGNING_KEY")
    assert store.secret_store.provider == "example-provider"
    assert store.secret_store.address == "https://store.example.invalid"
    document: dict[str, Any] = yaml.safe_load(
        (NODE_DIR / "overlay.example.yaml").read_text()
    )
    assert isinstance(document["signing_key_store"], dict)
    assert "value" not in document["signing_key_store"]


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("unexpected", "x"),
        ("folder", "shared/env"),
        ("key", ""),
    ],
)
def test_seam_malformed_signing_key_store_is_refused(mutation: str, value: str) -> None:
    document: dict[str, Any] = yaml.safe_load(
        (NODE_DIR / "overlay.example.yaml").read_text()
    )
    document["signing_key_store"][mutation] = value

    with pytest.raises(ValidationError):
        ModelBranchDeployCommandOverlay.model_validate(document)


def test_seam_signing_key_store_without_verify_env_key_is_refused() -> None:
    """``verify_env_key`` is required: a block without it refuses the load like
    every other absent overlay key, so an old block cannot silently keep the
    environment-wins behaviour."""
    document: dict[str, Any] = yaml.safe_load(
        (NODE_DIR / "overlay.example.yaml").read_text()
    )
    del document["signing_key_store"]["verify_env_key"]

    with pytest.raises(ValidationError):
        ModelBranchDeployCommandOverlay.model_validate(document)


def test_seam_a_non_bool_verify_env_key_is_refused() -> None:
    """StrictBool: a string (or any non-bool) refuses, so ``verify_env_key: "true"``
    cannot pass for the check."""
    document: dict[str, Any] = yaml.safe_load(
        (NODE_DIR / "overlay.example.yaml").read_text()
    )
    document["signing_key_store"]["verify_env_key"] = "true"

    with pytest.raises(ValidationError):
        ModelBranchDeployCommandOverlay.model_validate(document)


def test_seam_the_example_overlay_carries_verify_env_key() -> None:
    store = overlay().signing_key_store

    assert store is not None
    assert store.verify_env_key is True


def test_seam_contract_secret_dependencies_still_name_the_variable() -> None:
    document: dict[str, Any] = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    dependency = document["secret_dependencies"]["DEPLOY_AGENT_HMAC_SECRET"]

    assert dependency["required"] is True
    assert "hmac_secret_env" in dependency["description"]
    assert "signing_key_store" in dependency["description"]
    assert "OMN-24611" in document["metadata"]["related_tickets"]
    assert overlay().hmac_secret_env == SIGNING_ENV


def test_seam_the_entry_wires_the_store_reader_from_the_overlay_block(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The real ``entry.build_handler`` assembles the reader from the overlay's block.

    Only the broker dial, the bus and the store's network seam are replaced: the
    overlay load, the reader and the handler are the entry's own, so a build_handler
    that forgets to hand the handler its reader (the previous entry) refuses here.
    """
    monkeypatch.delenv(SIGNING_ENV, raising=False)
    bus = FakeBus()
    # The agent's expected key in this test environment is SIGNING_KEY, so the real key check the
    # entry runs passes only when the key it signed with is the one the store returned.
    fake = FakeSecretStore(SIGNING_KEY)
    store_block = overlay().signing_key_store
    assert store_block is not None
    asked_overlays: list[ModelSecretStoreOverlay] = []

    def _store_handler(*, overlay: ModelSecretStoreOverlay) -> HandlerSecretStore:
        asked_overlays.append(overlay)
        return HandlerSecretStore(store=fake, overlay=overlay, onex_home=tmp_path)

    def _broker(
        _node: str, *, environ: Mapping[str, str] | None = None
    ) -> ModelNodeBroker:
        del environ
        return ModelNodeBroker(
            kind="kafka",
            overlay_path=Path("overlay.yaml"),
            lane="lane-a",
            bootstrap_servers="broker-a:9092",
        )

    def _kafka_bus(_broker: ModelNodeBroker) -> FakeBus:
        return bus

    monkeypatch.setattr(entry, "resolve_dialable_broker", _broker)
    monkeypatch.setattr(entry, "_KafkaBus", _kafka_bus)
    monkeypatch.setattr(entry, "load_overlay", overlay)
    monkeypatch.setattr(entry, "HandlerSecretStore", _store_handler)

    code = entry.main(["--input", request(mode="deploy").model_dump_json()])

    assert code == 0, capsys.readouterr().err
    assert len(bus.published) == 1
    payload = json.loads(bus.published[0][2])
    body = {k: v for k, v in payload.items() if k != "_signature"}
    assert payload["_signature"] == _expected_signature(body, SIGNING_KEY)
    assert asked_overlays == [store_block.secret_store]
    assert fake.asked == [STORE_PATH]
