# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Valkey password read through a secret store reference instead of a plain environment variable (OMN-24112).

Golden: a reference the store resolves carries the password into the url, and a plain password variable still wins when it
is set. Error chain: a store that raises, a key the store does not hold, and a reference that is not a secret://onex path
each build a reader with no handler whose tally says why (the variable name only), so a step reads its source as before.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import SecretStr

from omnimarket.models.model_source_read_cache import (
    EnumSourceCacheOutcome,
    ModelSourceCacheOverlay,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers import (
    cached_source_reader_factory as factory,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.backend_valkey import (
    valkey_url,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader_factory import (
    build_cached_source_reader,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.password_reference import (
    resolve_password,
    split_store_reference,
)
from tests.nodes.node_source_read_cache_effect.support import key

pytestmark = pytest.mark.unit

REFERENCE = "secret://onex/shared/valkey/VALKEY_PASSWORD"
NOT_RESOLVED = "valkey: password reference in T_PW_REF did not resolve"
MEMORY = {
    "key_prefix": "t:",
    "ttl_backstop_seconds": 60,
    "clone_relpath_template": "{name}",
    "clone_revision_timeout_seconds": 5,
}
VALKEY_BLOCK = {
    "host_env": "T_HOST",
    "port_env": "T_PORT",
    "password_env": "T_PW",
    "password_ref_env": "T_PW_REF",
    "database": 3,
    "connect_timeout_seconds": 3,
    "operation_timeout_seconds": 5,
}
VALKEY = {**MEMORY, "valkey": VALKEY_BLOCK}


def _encode(value: list[int]) -> str:
    return ",".join(str(v) for v in value)


def _decode(text: str) -> list[int]:
    return [int(v) for v in text.split(",")]


class FakeStore:
    """A store that records its calls and holds one key."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def read(self, folder: str, name: str) -> SecretStr | None:
        self.calls.append((folder, name))
        if (folder, name) == ("/shared/valkey", "VALKEY_PASSWORD"):
            return SecretStr("pw-from-store")
        return None


class RaisingStore:
    """A store that is down: every read raises."""

    async def read(self, folder: str, name: str) -> SecretStr | None:
        raise RuntimeError("the store is down")


class SpyBackend:
    """Stands in for the Valkey backend class: records the url it is built with."""

    urls: list[str] = []

    def __init__(self, url: str, *args: Any, **kwargs: Any) -> None:
        SpyBackend.urls.append(url)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> type[SpyBackend]:
    SpyBackend.urls = []
    monkeypatch.setattr(factory, "BackendValkeySourceReadCache", SpyBackend)
    return SpyBackend


def _env(tmp_path: Path, doc: dict[str, Any], **extra: str) -> dict[str, str]:
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return {
        "ONEX_SOURCE_READ_CACHE_OVERLAY": str(path),
        "T_HOST": "valkey.example.invalid",
        "T_PORT": "16379",
        **extra,
    }


def test_golden_a_resolved_reference_carries_the_password(
    tmp_path: Path, spy: type[SpyBackend]
) -> None:
    store = FakeStore()
    reader = build_cached_source_reader(
        _env(tmp_path, VALKEY, T_PW_REF=REFERENCE), password_reader=store
    )
    assert spy.urls == [valkey_url("valkey.example.invalid", 16379, "pw-from-store", 3)]
    assert store.calls == [("/shared/valkey", "VALKEY_PASSWORD")]
    assert "pw-from-store" not in reader.tally.model_dump_json()


def test_golden_a_plain_password_variable_wins(
    tmp_path: Path, spy: type[SpyBackend]
) -> None:
    store = FakeStore()
    build_cached_source_reader(
        _env(tmp_path, VALKEY, T_PW="plain", T_PW_REF=REFERENCE),
        password_reader=store,
    )
    assert spy.urls == [valkey_url("valkey.example.invalid", 16379, "plain", 3)]
    assert store.calls == []


def test_golden_an_overlay_without_password_ref_env_still_validates(
    tmp_path: Path, spy: type[SpyBackend]
) -> None:
    block = {k: v for k, v in VALKEY_BLOCK.items() if k != "password_ref_env"}
    doc = {**MEMORY, "valkey": block}
    assert ModelSourceCacheOverlay.model_validate(doc).valkey is not None
    build_cached_source_reader(_env(tmp_path, doc))
    assert spy.urls == [valkey_url("valkey.example.invalid", 16379, None, 3)]


def test_error_chain_a_raising_store_disables_the_reader(
    tmp_path: Path, spy: type[SpyBackend]
) -> None:
    reader = build_cached_source_reader(
        _env(tmp_path, VALKEY, T_PW_REF=REFERENCE), password_reader=RaisingStore()
    )
    assert reader.tally.disabled_reason == NOT_RESOLVED
    assert spy.urls == []
    assert (
        reader.read(key(), lambda: [1], _encode, _decode)[1]
        == EnumSourceCacheOutcome.BYPASS
    )


def test_error_chain_a_key_the_store_does_not_hold_disables_the_reader(
    tmp_path: Path, spy: type[SpyBackend]
) -> None:
    other = "secret://onex/shared/valkey/OTHER_KEY"
    reader = build_cached_source_reader(
        _env(tmp_path, VALKEY, T_PW_REF=other), password_reader=FakeStore()
    )
    assert reader.tally.disabled_reason == NOT_RESOLVED
    assert spy.urls == []


def test_error_chain_a_reference_that_is_not_a_store_path_disables_the_reader(
    tmp_path: Path, spy: type[SpyBackend]
) -> None:
    store = FakeStore()
    reader = build_cached_source_reader(
        _env(tmp_path, VALKEY, T_PW_REF="hunter2"), password_reader=store
    )
    assert reader.tally.disabled_reason == NOT_RESOLVED
    assert "hunter2" not in reader.tally.model_dump_json()
    assert spy.urls == []
    assert store.calls == []


def test_split_store_reference_splits_a_full_path_and_a_bare_key() -> None:
    assert split_store_reference(REFERENCE) == ("/shared/valkey", "VALKEY_PASSWORD")
    assert split_store_reference("secret://onex/KEY") == ("/", "KEY")


@pytest.mark.parametrize(
    "bad",
    [
        "hunter2",
        "secret://onex/",
        "secret://other/a/B",
        "secret://onex/a b/K",
        "secret://onex/a//K",
    ],
)
def test_split_store_reference_refuses_shapes_it_does_not_accept(bad: str) -> None:
    with pytest.raises(ValueError, match="secret store reference") as excinfo:
        split_store_reference(bad)
    assert bad not in str(excinfo.value)


def test_resolve_password_resolves_through_a_store() -> None:
    assert resolve_password(REFERENCE, FakeStore()) == "pw-from-store"


def test_resolve_password_returns_empty_when_the_store_raises() -> None:
    assert resolve_password(REFERENCE, RaisingStore()) == ""


def test_resolve_password_returns_empty_for_a_bad_reference() -> None:
    assert resolve_password("hunter2", FakeStore()) == ""
