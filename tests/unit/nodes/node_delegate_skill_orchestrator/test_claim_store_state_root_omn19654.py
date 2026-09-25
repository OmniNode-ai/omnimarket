# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19654: the delegate-skill claim store lives under the configured state root.

On the onex-lab k3s lane the deployed ``node_delegate_skill_orchestrator`` ran
in a pod with ``readOnlyRootFilesystem: true`` and ``HOME=/``. With no
projection binding overlay on that pod, the OMN-18887 claim port fell back to a
local SQLite file whose location was derived from ``Path.home()``, so every
bus delivery tried to create ``/.omninode`` and terminalized
``OSError: [Errno 30] Read-only file system: '/.omninode'``. No delegation
completed on the lane.

These tests pin the fix: the fallback claim store is resolved from the
runtime's declared state root (``ONEX_STATE_DIR``, then ``ONEX_STATE_ROOT``),
never from the home directory, and an unconfigured state root is refused with a
named error at the moment a claim is attempted -- not silently defaulted, and
not at handler construction, where the bus-less CLI (which never claims) would
be broken for nothing.

The session conftest replaces ``default_claim_db_path`` on the claim module to
isolate the suite's claims. These tests need the REAL resolution, so they load
a fresh, unpatched copy of the module from its source file.
"""

from __future__ import annotations

import importlib.util
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_delegation_claim as _patched_claim_module,
)

_STATE_ENV_KEYS = ("ONEX_STATE_DIR", "ONEX_STATE_ROOT")
_BINDING_OVERLAY_ENV = "OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY"
_CLAIM_DB_RELATIVE = Path("delegation") / "delegation_claims.sqlite"


@pytest.fixture
def fresh_claim_module() -> Iterator[ModuleType]:
    """An unpatched instance of the claim module, loaded from its source file."""
    source = _patched_claim_module.__file__
    assert source is not None
    name = "_omn19654_fresh_port_delegation_claim"
    spec = importlib.util.spec_from_file_location(name, source)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(name, None)


@pytest.fixture
def readonly_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A HOME that cannot be written, standing in for ``HOME=/`` on a ro rootfs."""
    home = tmp_path / "readonly-home"
    home.mkdir()
    home.chmod(stat.S_IRUSR | stat.S_IXUSR)
    monkeypatch.setenv("HOME", str(home))
    # The evidence path is an import-time constant built from the HOME the
    # process started with. Point it into the read-only home too, so a
    # regression to a home-derived claim path fails here instead of writing
    # into the real home directory of whoever runs the suite.
    from omnimarket.projection import sqlite_database

    monkeypatch.setattr(
        sqlite_database,
        "_DEFAULT_EVIDENCE_DB_PATH",
        home / ".omninode" / "delegation" / "delegation.sqlite",
    )
    monkeypatch.delenv(_BINDING_OVERLAY_ENV, raising=False)
    for key in _STATE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    try:
        yield home
    finally:
        home.chmod(stat.S_IRWXU)


@pytest.mark.unit
def test_omninode_readonly_rootfs_claim_lands_under_onex_state_dir(
    fresh_claim_module: ModuleType,
    readonly_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state-dir"
    monkeypatch.setenv("ONEX_STATE_DIR", str(state_dir))

    port = fresh_claim_module.resolve_delegation_claim_store()
    outcome = port.claim(delivery_id=uuid4(), correlation_id=uuid4())

    assert outcome.won is True
    assert (state_dir / _CLAIM_DB_RELATIVE).is_file()
    assert not (readonly_home / ".omninode").exists()


@pytest.mark.unit
def test_omninode_readonly_rootfs_claim_uses_onex_state_root_when_dir_unset(
    fresh_claim_module: ModuleType,
    readonly_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The prod runtime ConfigMap declares ONEX_STATE_ROOT and not ONEX_STATE_DIR.
    state_root = tmp_path / "state-root"
    monkeypatch.setenv("ONEX_STATE_ROOT", str(state_root))

    assert fresh_claim_module.default_claim_db_path() == (
        state_root / _CLAIM_DB_RELATIVE
    )
    port = fresh_claim_module.resolve_delegation_claim_store()
    assert port.claim(delivery_id=uuid4(), correlation_id=uuid4()).won is True
    assert not (readonly_home / ".omninode").exists()


@pytest.mark.unit
def test_omninode_readonly_rootfs_onex_state_dir_takes_precedence(
    fresh_claim_module: ModuleType,
    readonly_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "dir"))
    monkeypatch.setenv("ONEX_STATE_ROOT", str(tmp_path / "root"))

    assert fresh_claim_module.default_claim_db_path() == (
        tmp_path / "dir" / _CLAIM_DB_RELATIVE
    )


@pytest.mark.unit
@pytest.mark.parametrize("blank", ["", "   "])
def test_omninode_readonly_rootfs_unconfigured_state_root_refuses_at_claim(
    fresh_claim_module: ModuleType,
    readonly_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    blank: str,
) -> None:
    for key in _STATE_ENV_KEYS:
        monkeypatch.setenv(key, blank)

    # Construction must not fail: the bus-less CLI builds this port and never
    # claims, because a claim is only attempted on a bus delivery.
    port = fresh_claim_module.resolve_delegation_claim_store()

    from omnimarket.config.state_root import OnexStateRootUnconfiguredError

    with pytest.raises(OnexStateRootUnconfiguredError) as excinfo:
        port.claim(delivery_id=uuid4(), correlation_id=uuid4())

    message = str(excinfo.value)
    for key in _STATE_ENV_KEYS:
        assert key in message
    assert not (readonly_home / ".omninode").exists()


@pytest.mark.unit
def test_omninode_readonly_rootfs_claim_store_is_durable_across_ports(
    fresh_claim_module: ModuleType,
    readonly_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A redelivery handled by a second port instance (a restarted consumer on
    # the same volume) must lose the claim the first instance won.
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "state"))
    delivery_id = uuid4()
    first = fresh_claim_module.resolve_delegation_claim_store()
    second = fresh_claim_module.resolve_delegation_claim_store()

    assert first.claim(delivery_id=delivery_id, correlation_id=uuid4()).won is True
    assert second.claim(delivery_id=delivery_id, correlation_id=uuid4()).won is False
