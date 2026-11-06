# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22478: a projection delivery parses each checked-in YAML file once."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.projection import delivery_cache
from omnimarket.projection.delivery_cache import (
    load_contract,
    load_topology_profile_cached,
)


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    delivery_cache._parse_contract.cache_clear()
    load_topology_profile_cached.cache_clear()


def _count_safe_load(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    real = yaml.safe_load

    def counting(stream: Any) -> Any:
        calls.append(1)
        return real(stream)

    monkeypatch.setattr(delivery_cache.yaml, "safe_load", counting)
    return calls


@pytest.mark.unit
class TestContractCache:
    def test_golden_one_parse_for_many_deliveries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "contract.yaml"
        path.write_text("name: node_x\nevent_bus:\n  subscribe_topics: [a, b]\n")
        calls = _count_safe_load(monkeypatch)

        results = [load_contract(path) for _ in range(50)]

        assert len(calls) == 1
        assert all(r["event_bus"]["subscribe_topics"] == ["a", "b"] for r in results)

    def test_each_caller_gets_its_own_copy(self, tmp_path: Path) -> None:
        path = tmp_path / "contract.yaml"
        path.write_text("name: node_x\nitems: [1]\n")

        first = load_contract(path)
        first["items"].append(2)
        first["name"] = "edited"

        assert load_contract(path) == {"name": "node_x", "items": [1]}

    def test_a_rewritten_file_is_read_fresh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "contract.yaml"
        path.write_text("name: first\n")
        assert load_contract(path)["name"] == "first"
        path.write_text("name: second-longer\n")

        assert load_contract(path)["name"] == "second-longer"

    def test_error_chain_a_missing_file_raises_every_time(self, tmp_path: Path) -> None:
        missing = tmp_path / "absent.yaml"
        for _ in range(2):
            with pytest.raises(FileNotFoundError):
                load_contract(missing)

    def test_error_chain_malformed_yaml_is_not_cached(self, tmp_path: Path) -> None:
        path = tmp_path / "contract.yaml"
        path.write_text("a: [unclosed\n")
        for _ in range(2):
            with pytest.raises(yaml.YAMLError):
                load_contract(path)


@pytest.mark.unit
class TestTopologyProfileCache:
    def test_golden_one_load_per_profile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import omnibase_infra.topology as topology

        loads: list[str] = []

        def fake_load(profile: str) -> object:
            loads.append(profile)
            return object()

        monkeypatch.setattr(topology, "load_topology_profile", fake_load)

        first = [load_topology_profile_cached("local") for _ in range(50)]
        other = load_topology_profile_cached("second-profile")

        assert loads == ["local", "second-profile"]
        assert all(m is first[0] for m in first)
        assert other is not first[0]

    def test_error_chain_a_failing_profile_is_retried_not_remembered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import omnibase_infra.topology as topology

        attempts: list[str] = []

        def failing(profile: str) -> object:
            attempts.append(profile)
            raise ValueError(f"Unsupported database topology profile '{profile}'")

        monkeypatch.setattr(topology, "load_topology_profile", failing)

        for _ in range(2):
            with pytest.raises(ValueError, match="Unsupported"):
                load_topology_profile_cached("nope")

        assert attempts == ["nope", "nope"]


@pytest.mark.unit
class TestRunnerConnectPathReadsTheProfileOnce:
    def test_golden_many_deliveries_load_the_profile_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio
        from types import SimpleNamespace

        import omnibase_infra.runtime.auto_wiring.handler_wiring as wiring
        import omnibase_infra.runtime.auto_wiring.standalone_projection_bindings as sb
        import omnibase_infra.topology as topology
        from pydantic import SecretStr

        import omnimarket.adapters.asyncpg_adapter as adapter_module
        from omnimarket.nodes.node_projection_live_events.handlers.handler_live_events import (
            HandlerLiveEventsProjectionRunner,
        )

        profile_loads: list[str] = []
        monkeypatch.setattr(
            topology,
            "load_topology_profile",
            lambda profile: profile_loads.append(profile) or object(),
        )

        async def no_secrets(_: Any) -> None:
            return None

        monkeypatch.setattr(wiring, "build_topology_secret_resolver", no_secrets)
        monkeypatch.setattr(
            sb,
            "resolve_standalone_projection_bindings",
            lambda *_a, **_k: SimpleNamespace(
                bindings={
                    "b": SimpleNamespace(dsn=SecretStr("postgresql://x"), principal="p")
                },
                watermark_binding="b",
            ),
        )

        class FakeAdapter:
            def __init__(self, dsn: str | None = None) -> None:
                self.dsn = dsn

            async def connect(self) -> None:
                return None

            async def close(self) -> None:
                return None

            async def fetchval(self, _query: str) -> str:
                return "p"

        monkeypatch.setattr(adapter_module, "AsyncpgAdapter", FakeAdapter)
        monkeypatch.setenv("ONEX_DATABASE_TOPOLOGY_PROFILE", "local")

        async def many() -> None:
            for _ in range(25):
                runner = HandlerLiveEventsProjectionRunner()
                runner._standalone_db_tables = tuple(
                    runner._contract["db_io"]["db_tables"]
                )
                await runner._connect_standalone_databases()
                await runner._close_standalone_databases()

        asyncio.run(many())

        assert profile_loads == ["local"]


@pytest.mark.unit
class TestControl:
    def test_control_without_the_cache_the_parse_count_is_one_per_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The injected break: bypass the cache and the counter reads 50, not 1."""
        path = tmp_path / "contract.yaml"
        path.write_text("name: node_x\n")
        calls = _count_safe_load(monkeypatch)
        monkeypatch.setattr(
            delivery_cache,
            "_parse_contract",
            delivery_cache._parse_contract.__wrapped__,
        )

        for _ in range(50):
            load_contract(path)

        assert len(calls) == 50
