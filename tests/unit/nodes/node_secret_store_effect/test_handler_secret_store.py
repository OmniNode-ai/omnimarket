# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The secret store effect's handler against a fake ProtocolSecretStore (OMN-20944)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from omnibase_spi.protocols.services import ProtocolSecretStore
from pydantic import SecretStr

from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)
from omnimarket.nodes.node_secret_store_effect.models import (
    EnumSecretStoreOperation,
    EnumSecretStoreOutcome,
    ModelSecretStoreOverlay,
    ModelSecretStoreRequest,
    ModelSecretStoreResult,
)

pytestmark = pytest.mark.unit

PLANTED = "planted-value-9f8a-do-not-leak"


class _FakeSecretStore:
    """A dict-backed ProtocolSecretStore that records every call."""

    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self.secrets = dict(secrets or {})
        self.calls: list[tuple[str, str]] = []
        self.raise_on: dict[str, BaseException] = {}

    def _maybe_raise(self, op: str) -> None:
        if op in self.raise_on:
            raise self.raise_on[op]

    async def get_secret(self, key: str) -> str | None:
        self.calls.append(("get", key))
        self._maybe_raise("get")
        return self.secrets.get(key)

    async def set_secret(self, key: str, value: str) -> bool:
        self.calls.append(("set", key))
        self._maybe_raise("set")
        if key in self.secrets:
            return False
        self.secrets[key] = value
        return True

    async def delete_secret(self, key: str) -> bool:
        raise RuntimeError("not offered")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        self.calls.append(("list", prefix or "/"))
        self._maybe_raise("list")
        return sorted(self.secrets)

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float = 30.0) -> None:
        return None


def _overlay(root: str = "/") -> ModelSecretStoreOverlay:
    return ModelSecretStoreOverlay(
        provider="fake",
        address="http://127.0.0.1:9",
        project_id="00000000-0000-0000-0000-000000000001",
        environment="dev",
        root_folder=root,
        client_id_ref="id-ref",
        client_secret_ref="secret-ref",
    )


def _handler(store: _FakeSecretStore, root: str = "/") -> HandlerSecretStore:
    return HandlerSecretStore(store=store, overlay=_overlay(root))


def _assert_no_value(
    result: ModelSecretStoreResult, caplog: pytest.LogCaptureFixture
) -> None:
    assert PLANTED not in result.model_dump_json()
    assert PLANTED not in repr(result)
    assert all(PLANTED not in r.getMessage() for r in caplog.records)


def test_fake_store_satisfies_the_spi_protocol() -> None:
    assert isinstance(_FakeSecretStore(), ProtocolSecretStore)


async def test_resolve_by_folder_and_key_returns_the_value_in_process_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    store = _FakeSecretStore({"/lab/TEST_KEY": PLANTED})
    result = await _handler(store).handle(
        ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.RESOLVE, folder="/lab", key="TEST_KEY"
        )
    )
    assert result.outcome is EnumSecretStoreOutcome.RESOLVED
    assert result.value is not None
    assert result.value.get_secret_value() == PLANTED
    assert "value" not in result.model_dump()
    _assert_no_value(result, caplog)


async def test_resolve_a_plain_reference() -> None:
    store = _FakeSecretStore({"/lab/sub/TEST_KEY": PLANTED})
    result = await _handler(store).handle(
        ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.RESOLVE,
            reference="secret://onex/lab/sub/TEST_KEY",
        )
    )
    assert result.outcome is EnumSecretStoreOutcome.RESOLVED
    assert (result.folder, result.key) == ("/lab/sub", "TEST_KEY")


async def test_resolve_a_captured_reference_through_the_contract_namespace() -> None:
    digest = "ab" * 32
    store = _FakeSecretStore({f"/captured/CAPTURED_{digest}": PLANTED})
    result = await _handler(store).handle(
        ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.RESOLVE,
            reference=f"secret://onex/captured/{digest}",
        )
    )
    assert result.outcome is EnumSecretStoreOutcome.RESOLVED
    assert result.key == f"CAPTURED_{digest}"


async def test_folders_are_relative_to_the_overlay_root() -> None:
    store = _FakeSecretStore({"/onex/lab/TEST_KEY": PLANTED})
    result = await _handler(store, root="/onex").handle(
        ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.RESOLVE, folder="/lab", key="TEST_KEY"
        )
    )
    assert result.outcome is EnumSecretStoreOutcome.RESOLVED
    assert result.folder == "/onex/lab"


async def test_resolve_a_missing_key_is_not_found() -> None:
    result = await _handler(_FakeSecretStore()).handle(
        ModelSecretStoreRequest(operation=EnumSecretStoreOperation.RESOLVE, key="NOPE")
    )
    assert result.outcome is EnumSecretStoreOutcome.NOT_FOUND
    assert result.value is None


async def test_list_returns_key_names_of_that_folder_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    store = _FakeSecretStore(
        {
            "/lab/A": PLANTED,
            "/lab/B": PLANTED,
            "/lab/deeper/C": PLANTED,
            "/other/D": PLANTED,
        }
    )
    result = await _handler(store).handle(
        ModelSecretStoreRequest(operation=EnumSecretStoreOperation.LIST, folder="/lab")
    )
    assert result.outcome is EnumSecretStoreOutcome.LISTED
    assert result.keys == ("A", "B")
    _assert_no_value(result, caplog)


async def test_create_writes_a_new_key(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    store = _FakeSecretStore()
    result = await _handler(store).handle(
        ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.CREATE,
            folder="/captured",
            key="NEW_KEY",
            value=SecretStr(PLANTED),
        )
    )
    assert result.outcome is EnumSecretStoreOutcome.CREATED
    assert store.secrets["/captured/NEW_KEY"] == PLANTED
    _assert_no_value(result, caplog)


async def test_create_on_an_existing_key_reports_exists_without_read_or_update() -> (
    None
):
    store = _FakeSecretStore({"/captured/KEY": "original"})
    result = await _handler(store).handle(
        ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.CREATE,
            folder="/captured",
            key="KEY",
            value=SecretStr(PLANTED),
        )
    )
    assert result.outcome is EnumSecretStoreOutcome.EXISTS
    assert store.secrets["/captured/KEY"] == "original"
    assert [op for op, _ in store.calls] == ["set"]


async def test_create_sent_as_an_event_has_no_value_and_is_refused() -> None:
    request = ModelSecretStoreRequest(
        operation=EnumSecretStoreOperation.CREATE, key="KEY", value=SecretStr(PLANTED)
    )
    wire = request.model_dump_json()
    assert PLANTED not in wire
    store = _FakeSecretStore()
    result = await _handler(store).handle(
        ModelSecretStoreRequest.model_validate_json(wire)
    )
    assert result.outcome is EnumSecretStoreOutcome.VALUE_MISSING
    assert store.calls == []


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (
            PermissionError("create refused with HTTP 403"),
            EnumSecretStoreOutcome.REFUSED,
        ),
        (
            ConnectionError("create failed: ConnectError"),
            EnumSecretStoreOutcome.UNREACHABLE,
        ),
        (ValueError(f"echo {PLANTED}"), EnumSecretStoreOutcome.FAILED),
    ],
)
async def test_create_failures_are_typed_outcomes_with_no_value(
    error: BaseException,
    outcome: EnumSecretStoreOutcome,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    store = _FakeSecretStore()
    store.raise_on["set"] = error
    result = await _handler(store).handle(
        ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.CREATE,
            key="KEY",
            value=SecretStr(PLANTED),
        )
    )
    assert result.outcome is outcome
    _assert_no_value(result, caplog)


@pytest.mark.parametrize(
    "request_fields",
    [
        {"operation": "resolve"},
        {"operation": "resolve", "key": "A", "reference": "secret://onex/A"},
        {"operation": "resolve", "reference": "secret://onex/x/../A"},
        {"operation": "resolve", "reference": "https://elsewhere/A"},
        {"operation": "resolve", "key": "a/b"},
        {"operation": "resolve", "key": "A", "folder": "relative"},
        {"operation": "list", "key": "A"},
    ],
)
async def test_malformed_requests_are_invalid_and_touch_no_store(
    request_fields: dict[str, str],
) -> None:
    store = _FakeSecretStore()
    result = await _handler(store).handle(
        ModelSecretStoreRequest.model_validate(request_fields)
    )
    assert result.outcome is EnumSecretStoreOutcome.INVALID_REQUEST
    assert store.calls == []


async def test_no_overlay_block_is_not_configured(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("gateway: {}\n")
    result = await HandlerSecretStore(onex_home=tmp_path).handle(
        ModelSecretStoreRequest(operation=EnumSecretStoreOperation.LIST)
    )
    assert result.outcome is EnumSecretStoreOutcome.NOT_CONFIGURED


async def test_an_unknown_provider_is_misconfigured(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        "secret_store:\n"
        "  provider: nothing-we-ship\n"
        "  address: http://127.0.0.1:9\n"
        "  project_id: 00000000-0000-0000-0000-000000000001\n"
        "  environment: dev\n"
        "  client_id_ref: id-ref\n"
        "  client_secret_ref: secret-ref\n"
    )
    creds = tmp_path / "credentials.json"
    creds.write_text('{"id-ref": "x", "secret-ref": "y"}')
    creds.chmod(0o600)
    result = await HandlerSecretStore(onex_home=tmp_path).handle(
        ModelSecretStoreRequest(operation=EnumSecretStoreOperation.LIST)
    )
    assert result.outcome is EnumSecretStoreOutcome.MISCONFIGURED
    assert "nothing-we-ship" in (result.detail or "")
