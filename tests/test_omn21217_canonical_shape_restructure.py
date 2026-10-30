# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21217: the restructure to the canonical-file-shape rules keeps behaviour.

The canonical-file-shape check refuses a ``handler_*.py`` module no
``contract.yaml`` binds (handler_outside_node) and a baselined file that grows
(baselined-file-growth). This change binds the tenant credential node's three
handler modules from its contract and moves new code out of seven baselined
files. These tests hold what must not change:

* the three modules are bound, read the way the check reads a contract: a
  ``handler`` mapping, at any depth, whose ``module`` is the dotted path;
* the node's ``handler_routing`` still sends all four of its topics to the
  runtime writer and nothing else, so the binding block routes nothing;
* every moved name is still importable from the module it left, and is the
  very object the new module defines, so no caller changes;
* the command's fold still writes the store ``cli_secret`` hands it.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[1]
_NODE = _REPO / "src/omnimarket/nodes/node_projection_tenant_credentials"
_HANDLERS = "omnimarket.nodes.node_projection_tenant_credentials.handlers"
_RUNTIME_WRITER = f"{_HANDLERS}.handler_runtime_writer"

_BOUND_MODULES = {
    f"{_HANDLERS}.handler_credential_route_write": "HandlerCredentialRouteWrite",
    f"{_HANDLERS}.handler_tenant_credentials_projection": (
        "HandlerTenantCredentialsProjectionRunner"
    ),
    f"{_HANDLERS}.handler_tenant_credentials_store": "apply_credential_registered",
}

_ROUTED_TOPICS = [
    "onex.evt.omnimarket.credential-registered.v1",
    "onex.evt.omnimarket.credential-revoked.v1",
    "onex.evt.omnimarket.tenant-credential-state.v1",
    "onex.evt.omnimarket.tenant-routing-overlay-state.v1",
    # OMN-19996: the delegate-skill terminals that move a key's last-used time.
    "onex.evt.omnimarket.delegate-skill-completed.v1",
    "onex.evt.omnimarket.delegate-skill-failed.v1",
]

_PUBLISHER = "omnimarket.projection.credential_publisher"
_RESOLVER = "omnimarket.routing.tenant_overlay_resolver"
_SQLITE = "omnimarket.projection.sqlite_database"
_POSTGRES = "omnimarket.projection.postgres_sync_database"
_STORES = "omnimarket.nodes.node_projection_tenant_credentials.stores"
_KEYS = "omnimarket.nodes.node_delegation_routing_reducer.tenant_key_selection"
_REQUEST = "omnimarket.models.model_inference_credential_create_request"
_COMPACTION = "omnimarket.handlers.registry_compaction_source"
_PROTOCOLS = "omnimarket.protocols.protocol_projection_database_sync"
_PROTOCOL_DATABASE = "omnimarket.projection.protocol_database"

# (module the name left, name, module that now defines it)
_MOVED = [
    (_PUBLISHER, "LAB_HOST_NAME_PATTERN", _REQUEST),
    (_PUBLISHER, "ModelInferenceCredentialCreateRequest", _REQUEST),
    (_PUBLISHER, "ProviderCatalogueUnavailableError", _REQUEST),
    (_PROTOCOL_DATABASE, "ProtocolProjectionDatabaseSync", _PROTOCOLS),
    (_PROTOCOL_DATABASE, "ProtocolProjectionAttestedWrite", _PROTOCOLS),
    (_PROTOCOL_DATABASE, "DatabaseAdapter", _PROTOCOLS),
    (_RESOLVER, "TENANT_CREDENTIALS_TABLE", _KEYS),
    (_RESOLVER, "ProtocolTenantOverlayReader", _KEYS),
    (_RESOLVER, "_selected_key_model", _KEYS),
    (_RESOLVER, "_select_pool_secret_ref", _KEYS),
    (_RESOLVER, "_not_expired", _KEYS),
    (_SQLITE, "_TENANT_INFERENCE_CREDENTIALS_DDL", _STORES),
    (_SQLITE, "_TENANT_INFERENCE_CREDENTIALS_ADDED_COLUMNS", _STORES),
    (_SQLITE, "_DELEGATION_ROUTING_TENANT_OVERLAY_DDL", _STORES),
    (_SQLITE, "_CREDENTIAL_ROUTE_VERSIONS_STEP", _STORES),
    (_SQLITE, "_CREDENTIAL_CHOSEN_MODELS_STEP", _STORES),
]

# (adapter module, adapter class, mixin class, method)
_MIXED_IN = [
    (
        _SQLITE,
        "SqliteDatabaseAdapter",
        "CredentialSqliteStore",
        "replace_credential_route",
    ),
    (_SQLITE, "SqliteDatabaseAdapter", "CredentialSqliteStore", "write_credential_row"),
    (
        _SQLITE,
        "SqliteDatabaseAdapter",
        "CredentialSqliteStore",
        "_fill_credential_route_versions",
    ),
    (
        _SQLITE,
        "SqliteDatabaseAdapter",
        "CredentialSqliteStore",
        "_backfill_credential_chosen_models",
    ),
    (
        _SQLITE,
        "SqliteDatabaseAdapter",
        "CredentialSqliteStore",
        "record_credential_last_used",
    ),
    (
        _POSTGRES,
        "PostgresSyncProjectionAdapter",
        "CredentialPgStore",
        "replace_credential_route",
    ),
    (
        _POSTGRES,
        "PostgresSyncProjectionAdapter",
        "CredentialPgStore",
        "write_credential_row",
    ),
    (
        _POSTGRES,
        "PostgresSyncProjectionAdapter",
        "CredentialPgStore",
        "record_credential_last_used",
    ),
]

_GATE_NAMES = (
    "GateInputError",
    "_served_exposures",
    "_topic_declarations",
    "_declared_cleanup_policies",
    "_is_compacted",
    "_projection_imports",
    "_node_projection_importers",
)


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _handler_bindings(node: object) -> list[dict[str, Any]]:
    """Every ``handler`` mapping with a string ``module``, at any depth."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if (
                key == "handler"
                and isinstance(value, dict)
                and isinstance(value.get("module"), str)
            ):
                found.append(value)
            found.extend(_handler_bindings(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_handler_bindings(item))
    return found


@pytest.mark.unit
def test_the_three_credential_handler_modules_are_bound_by_the_node_contract() -> None:
    bound = {b["module"]: b.get("name") for b in _handler_bindings(_contract())}
    missing = sorted(set(_BOUND_MODULES) - set(bound))
    assert not missing, f"handler modules no contract binds: {missing}"
    for module, name in _BOUND_MODULES.items():
        assert bound[module] == name
        assert hasattr(importlib.import_module(module), name), (module, name)


@pytest.mark.unit
def test_handler_routing_still_sends_every_topic_to_the_runtime_writer_only() -> None:
    contract = _contract()
    entries = contract["handler_routing"]["handlers"]
    assert [entry["topic"] for entry in entries] == _ROUTED_TOPICS
    for entry in entries:
        assert entry["handler"] == {
            "name": "TenantCredentialsProjectionWriter",
            "module": _RUNTIME_WRITER,
        }
        assert entry["handler_module"] == _RUNTIME_WRITER
    assert contract["handler"]["module"] == _RUNTIME_WRITER


@pytest.mark.unit
@pytest.mark.parametrize(("old", "name", "new"), _MOVED)
def test_a_moved_name_is_the_same_object_from_its_old_module(
    old: str, name: str, new: str
) -> None:
    moved = getattr(importlib.import_module(new), name)
    assert getattr(importlib.import_module(old), name) is moved


@pytest.mark.unit
@pytest.mark.parametrize(("module", "adapter", "mixin", "method"), _MIXED_IN)
def test_an_adapter_method_is_the_mixin_method(
    module: str, adapter: str, mixin: str, method: str
) -> None:
    mixin_cls = getattr(importlib.import_module(_STORES), mixin)
    adapter_cls = getattr(importlib.import_module(module), adapter)
    assert issubclass(adapter_cls, mixin_cls)
    assert method not in vars(adapter_cls), f"{adapter}.{method} is still its own"
    assert getattr(adapter_cls, method) is getattr(mixin_cls, method)


@pytest.mark.unit
def test_the_gate_script_reads_compaction_through_the_moved_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = importlib.util.spec_from_file_location(
        "check_registry_projection_reconcile_omn21217",
        _REPO / "scripts/ci/check_registry_projection_reconcile.py",
    )
    assert spec is not None
    assert spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    # A dataclass resolves its annotations through sys.modules.
    monkeypatch.setitem(sys.modules, spec.name, gate)
    spec.loader.exec_module(gate)
    source = importlib.import_module(_COMPACTION)
    for name in _GATE_NAMES:
        assert getattr(gate, name) is getattr(source, name), name


@pytest.mark.unit
def test_the_cli_fold_writes_the_store_the_command_hands_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.cli import cli_secret
    from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

    db_path = tmp_path / "catalog.sqlite"
    monkeypatch.setattr(
        cli_secret, "_tenant_key_store", lambda: SimpleNamespace(db_path=db_path)
    )
    response = SimpleNamespace(
        provider="openrouter",
        name="key-one",
        api_key_ref="secret://onex/tenant/t1/openrouter/abc",
        plan=None,
        model="m-a",
        models=["m-a"],
        expected_version=None,
        expires_at=None,
        bound_host=None,
        workspace_id=None,
    )
    cli_secret._fold_registered_tenant_key(response, "t1")
    rows = SqliteDatabaseAdapter(db_path).query(
        "tenant_inference_credentials", {"api_key_ref": response.api_key_ref}
    )
    assert [row["tenant_id"] for row in rows] == ["t1"]
