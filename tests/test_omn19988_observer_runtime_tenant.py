# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19988: the mode-2 observer takes the runtime's configured tenant.

The compose bundle never runs ``onex local init``; it carries its tenant only as
``ONEX_TENANT_ID``, which the bundle's seed step records in the tenant registry
mirror. On the 2026-10-21 lab run the observer asked for a minted install
identity, found none and refused every tick with ``identity_absent``, so no row
could appear without a hand step. When the install has no identity the observer
now reports for the runtime's configured tenant, and the projection writer
confirms that tenant against the registry mirror before it stores a row.

The tests run the real tenant resolver over a temporary identity store and the
real settings reader over the process environment. Only the network prober is a
stand-in.

Failure modes each test is written against:

* F1 a stock bundle (configured tenant, no install identity) still refuses, so
  no row ever appears without a hand step;
* F2 an install with neither identity nor configuration reports under some
  default instead of refusing as before;
* F3 an install identity and a different configured tenant: the observer picks
  one silently, or refuses without saying which two disagree, or still probes;
* F4 a configured tenant the registry does not know is stored (writer half,
  proven in test_omn19988_local_environment_writer_registry.py);
* F5 a mode-1 install (minted identity, `onex dashboard`) reports under the
  configured tenant, or an observer that has an identity reports under another;
* F6 a configured value that is neither a uuid nor a slug is taken as a tenant;
* F7 the over-refusal control: a configured tenant that names the same install
  as its identity (by uuid or by slug) is refused as a disagreement.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.config.settings import get_settings
from omnimarket.enums.enum_local_service_state import EnumLocalServiceState
from omnimarket.local_deployment import tenant_identity
from omnimarket.local_deployment.tenant_identity import (
    mint_local_tenant_identity,
    reset_local_tenant_identity_cache,
)
from omnimarket.nodes.node_local_environment_observe_effect.handlers import (
    handler_local_environment_observe as observer,
)

pytestmark = pytest.mark.unit

SLUG = "local-7eac03d77ba9"
CONFIGURED_UUID = "00000000-0000-4000-8000-0000000000a2"
INSTALLED_UUID = UUID("19988000-0000-4000-8000-0000000000aa")
INSTALLED_SLUG = "installed-slug"
NOW = datetime(2026, 10, 22, 1, tzinfo=UTC)


class _Prober:
    def __init__(self) -> None:
        self.calls: list[observer.ModelProbeTarget] = []

    async def probe(
        self, target: observer.ModelProbeTarget, timeout: float
    ) -> observer.ModelProbeResult:
        self.calls.append(target)
        return observer.ModelProbeResult(
            observed_state=EnumLocalServiceState.RUNNING, reason="probed"
        )


@pytest.fixture
def catalog(tmp_path: Path) -> Path:
    root = tmp_path / "catalog"
    (root / "services").mkdir(parents=True)
    (root / "bundles.yaml").write_text(yaml.safe_dump({"local": {"services": ["tcp"]}}))
    (root / "services" / "tcp.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "tcp",
                "image": "test:local",
                "layer": "runtime",
                "ports": {"external": 19000, "internal": 8000},
            }
        )
    )
    return root


@pytest.fixture
def identity_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """An empty install identity store, not the developer's own."""
    store = tmp_path / "install" / "delegation.sqlite"
    monkeypatch.setattr(tenant_identity, "install_identity_store", lambda: store)
    reset_local_tenant_identity_cache()
    yield store
    reset_local_tenant_identity_cache()


@pytest.fixture
def configure(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[str | None], None]]:
    """Set the runtime's configured tenant the way the bundle does: the env."""

    def _set(value: str | None) -> None:
        if value is None:
            monkeypatch.delenv("ONEX_TENANT_ID", raising=False)
        else:
            monkeypatch.setenv("ONEX_TENANT_ID", value)
        get_settings.cache_clear()

    _set(None)
    yield _set
    monkeypatch.undo()
    get_settings.cache_clear()


def _install_identity(store: Path) -> None:
    mint_local_tenant_identity(
        db_path=store, tenant_uuid=INSTALLED_UUID, tenant_slug=INSTALLED_SLUG
    )
    reset_local_tenant_identity_cache()


def _tick(
    catalog: Path, prober: _Prober
) -> tuple[list[str], tuple[str, ...], list[object]]:
    config = observer.ModelLocalEnvironmentObserveConfig(catalog_dir=str(catalog))
    handler = observer.HandlerLocalEnvironmentObserve(prober=prober, config=config)
    now = NOW
    tick = ModelRuntimeTick(
        now=now,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid4(),
        scheduler_id="omn19988-tenant-test",
        tick_interval_ms=1000,
    )
    out = asyncio.run(handler.handle(tick))
    tenants = [e.tenant_id for e in out.events]
    return tenants, tuple(out.logs), list(out.events)


@pytest.mark.parametrize("configured", [SLUG, CONFIGURED_UUID])
def test_a_configured_runtime_tenant_and_no_install_identity_reports_for_that_tenant(
    catalog: Path,
    identity_store: Path,
    configure: Callable[[str | None], None],
    configured: str,
) -> None:
    configure(configured)
    prober = _Prober()
    tenants, logs, _events = _tick(catalog, prober)
    assert tenants == [configured], (tenants, logs)
    assert [c.service for c in prober.calls] == ["tcp"]
    assert not any("identity_" in line for line in logs)


def test_neither_a_configured_tenant_nor_an_install_identity_refuses_as_before(
    catalog: Path, identity_store: Path, configure: Callable[[str | None], None]
) -> None:
    configure(None)
    prober = _Prober()
    tenants, logs, _ = _tick(catalog, prober)
    assert tenants == []
    assert "identity_absent" in " ".join(logs)
    assert prober.calls == []


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_configured_tenant_is_not_a_configuration(
    catalog: Path,
    identity_store: Path,
    configure: Callable[[str | None], None],
    blank: str,
) -> None:
    configure(blank)
    prober = _Prober()
    tenants, logs, _ = _tick(catalog, prober)
    assert tenants == []
    assert "identity_absent" in " ".join(logs)
    assert prober.calls == []


@pytest.mark.parametrize(
    "configured", ["Not A Slug", "UPPER-CASE", CONFIGURED_UUID.upper(), "-leading"]
)
def test_a_configured_value_that_is_neither_uuid_nor_slug_refuses_and_probes_nothing(
    catalog: Path,
    identity_store: Path,
    configure: Callable[[str | None], None],
    configured: str,
) -> None:
    configure(configured)
    prober = _Prober()
    tenants, logs, _ = _tick(catalog, prober)
    assert tenants == []
    assert "identity_malformed" in " ".join(logs)
    assert prober.calls == []


@pytest.mark.parametrize("configured", [SLUG, CONFIGURED_UUID])
def test_an_install_identity_and_a_different_configured_tenant_refuse_naming_both(
    catalog: Path,
    identity_store: Path,
    configure: Callable[[str | None], None],
    configured: str,
) -> None:
    _install_identity(identity_store)
    configure(configured)
    prober = _Prober()
    tenants, logs, _ = _tick(catalog, prober)
    assert tenants == []
    text = " ".join(logs)
    assert configured in text
    assert str(INSTALLED_UUID) in text
    assert "identity_conflict" in text
    assert prober.calls == []


@pytest.mark.parametrize("configured", [str(INSTALLED_UUID), INSTALLED_SLUG])
def test_a_configured_tenant_naming_the_installed_identity_agrees_and_reports_its_uuid(
    catalog: Path,
    identity_store: Path,
    configure: Callable[[str | None], None],
    configured: str,
) -> None:
    _install_identity(identity_store)
    configure(configured)
    prober = _Prober()
    tenants, logs, _ = _tick(catalog, prober)
    assert tenants == [str(INSTALLED_UUID)], logs


def test_mode_two_with_a_minted_identity_and_no_configured_tenant_is_unchanged(
    catalog: Path, identity_store: Path, configure: Callable[[str | None], None]
) -> None:
    _install_identity(identity_store)
    configure(None)
    prober = _Prober()
    tenants, _, _ = _tick(catalog, prober)
    assert tenants == [str(INSTALLED_UUID)]
