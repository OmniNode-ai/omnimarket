# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lab host resolves a declared provider ref from the secret store first (OMN-19998).

The local entry for ``llm.openrouter.api_key`` was copied host to host by hand (OMN-20266), which
made each machine's local store the source of truth. The store is the source: a host with its machine
identity asks the store first, and its local entry is only a cache. Every case below drives the real
``_select_resolver`` and ``resolve_api_key_with_source`` with only the store and the local store faked.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import omnimarket.handlers.host_provider_secret_store as host_store
import omnimarket.inference.secret_store_resolver as resolver
from omnimarket.enums.enum_secret_source import EnumSecretSource
from omnimarket.inference.secret_store_resolver import (
    LocalSecretNotRegisteredError,
    resolve_api_key_with_source,
)

pytestmark = pytest.mark.unit

REF = "llm.openrouter.api_key"
BOOTSTRAP = {
    "INFISICAL_ADDR": "http://store.invalid:8880",
    "INFISICAL_CLIENT_ID": "id-for-test",
    "INFISICAL_CLIENT_SECRET": "secret-for-test",
    "INFISICAL_PROJECT_ID": "872e00f4-1279-49a4-8e16-a2bd961f4996",
    "INFISICAL_ENVIRONMENT_SLUG": "dev",
}


class FakeStore:
    def __init__(self, values: dict[str, str] | None = None, *, fail: bool = False):
        self.values = values or {}
        self.fail = fail
        self.asked: list[str] = []

    async def get_secret(self, key: str) -> str | None:
        self.asked.append(key)
        if self.fail:
            raise ConnectionError("store unreachable")
        return self.values.get(key)


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A host with no ambient identity, an empty home, and a faked store and local store."""
    for name in host_store.INFISICAL_BOOTSTRAP_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    resolver.clear_secret_store_resolver_cache()
    host_store.clear_host_provider_cache()
    store = FakeStore({REF: "from-the-store"})
    local = FakeStore({REF: "from-the-local-cache"})
    monkeypatch.setattr(host_store, "_host_provider_store", lambda: store)
    monkeypatch.setattr(host_store, "LocalByokCredentialStore", lambda: local)
    monkeypatch.setattr(resolver, "LocalByokCredentialStore", lambda: local)
    yield store, local, tmp_path
    monkeypatch.undo()
    resolver.clear_secret_store_resolver_cache()
    host_store.clear_host_provider_cache()


def write_host_identity(home: Path) -> None:
    (home / ".omnibase").mkdir()
    (home / ".omnibase" / ".env").write_text(
        "".join(f"{k}={v}\n" for k, v in BOOTSTRAP.items()) + "UNRELATED_NAME=x\n"
    )


def test_store_answers_before_the_local_entry_on_a_host_with_its_identity(host) -> None:
    store, local, home = host
    write_host_identity(home)
    value, source = resolve_api_key_with_source(REF)
    assert value is not None
    assert value.get_secret_value() == "from-the-store"
    assert source is EnumSecretSource.LANE_SECRET_STORE
    assert store.asked == [REF]
    assert local.asked == []


def test_negative_control_without_identity_the_local_store_alone_answers(host) -> None:
    store, _, _ = host
    value, source = resolve_api_key_with_source(REF)
    assert value is not None
    assert value.get_secret_value() == "from-the-local-cache"
    assert source is EnumSecretSource.LOCAL_STORE
    assert store.asked == []


def test_negative_control_without_identity_and_without_entry_is_still_refused(
    host,
) -> None:
    _, local, _ = host
    local.values.clear()
    with pytest.raises(LocalSecretNotRegisteredError):
        resolve_api_key_with_source(REF)


def test_local_entry_answers_only_when_the_store_holds_nothing(host) -> None:
    store, _, home = host
    write_host_identity(home)
    store.values.clear()
    value, _ = resolve_api_key_with_source(REF)
    assert value is not None
    assert value.get_secret_value() == "from-the-local-cache"


def test_local_entry_answers_when_the_store_is_unreachable(host) -> None:
    store, _, home = host
    write_host_identity(home)
    store.fail = True
    value, _ = resolve_api_key_with_source(REF)
    assert value is not None
    assert value.get_secret_value() == "from-the-local-cache"


def test_a_ref_the_map_does_not_hold_never_asks_the_store(host) -> None:
    store, local, home = host
    write_host_identity(home)
    local.values["llm.glm.api_key"] = "glm-local"
    value, source = resolve_api_key_with_source("llm.glm.api_key")
    assert value is not None
    assert value.get_secret_value() == "glm-local"
    assert source is EnumSecretSource.LOCAL_STORE
    assert store.asked == []


def test_a_provider_ref_never_falls_back_to_the_environment(
    host, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, local, home = host
    write_host_identity(home)
    store.values.clear()
    local.values.clear()
    monkeypatch.setenv("LLM_OPENROUTER_API_KEY", "from-env")
    with pytest.raises(LocalSecretNotRegisteredError):
        resolve_api_key_with_source(REF, env_var_fallback="LLM_OPENROUTER_API_KEY")


def test_the_host_file_supplies_only_the_five_bootstrap_names(host) -> None:
    _, _, home = host
    write_host_identity(home)
    assert host_store._host_bootstrap_file_values() == BOOTSTRAP


def test_the_process_environment_wins_over_the_host_file(
    host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, home = host
    write_host_identity(home)
    monkeypatch.setenv("INFISICAL_ADDR", "http://other.invalid:1")
    assert (
        host_store.bootstrap_values(os.environ)["INFISICAL_ADDR"]
        == "http://other.invalid:1"
    )
    assert (
        host_store.bootstrap_values(os.environ)["INFISICAL_CLIENT_ID"] == "id-for-test"
    )


def test_the_packaged_map_holds_the_openrouter_ref() -> None:
    resolver.clear_secret_store_resolver_cache()
    host_store.clear_host_provider_cache()
    assert (
        host_store.host_provider_sources()[REF]
        == "/lab-provider-keys/OPENROUTER_API_KEY"
    )
