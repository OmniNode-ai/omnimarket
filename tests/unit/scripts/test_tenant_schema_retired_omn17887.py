# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17887: the ``tenant`` schema is retired; ``public`` is the tenant schema.

Operator ruling 2026-09-24: no ``tenant`` Postgres schema will be built. The
Tenant domain (ADR-0027) lives physically in ``public`` for good, and
``platform_catalog`` stays as the migration-ledger schema. Every declaration
therefore names ``public`` -- the schema its relation actually lives in -- and
the typed topology resolves ``application``.``public`` to the TENANT domain on
every shipped profile, so domain and binding are unchanged.

These tests pin that: no omnimarket declaration may name ``tenant`` again, the
relation inventory classifies ``public`` as TENANT and refuses ``tenant`` as a
schema that does not resolve.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
NODES_ROOT = REPO_ROOT / "src" / "omnimarket" / "nodes"
MANIFEST = REPO_ROOT / "scripts" / "application-relation-ownership.yaml"
SCRIPT = REPO_ROOT / "scripts" / "generate_application_relation_inventory.py"
INVENTORY = REPO_ROOT / "docs" / "evidence" / "OMN-15423-relation-inventory.json"

pytestmark = pytest.mark.unit


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "application_relation_inventory_omn17887", SCRIPT
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schema_values(node: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Yield (yaml path, value) for every ``schema`` key anywhere in a document."""
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}"
            if key == "schema" and isinstance(value, str):
                yield child, value
            yield from _schema_values(value, child)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _schema_values(value, f"{path}[{index}]")


def _tenant_schema_paths(document: Any) -> list[str]:
    return [path for path, value in _schema_values(document) if value == "tenant"]


def test_schema_walker_sees_a_nested_tenant_declaration() -> None:
    """Positive control: the walker finds a declaration at any depth."""
    document = yaml.safe_load(
        "db_io:\n"
        "  db_tables:\n"
        "    - name: example\n"
        "      schema: tenant\n"
        "projection_api:\n"
        "  exposures:\n"
        "    - schema: public\n"
    )
    assert _tenant_schema_paths(document) == [".db_io.db_tables[0].schema"]


def test_no_node_contract_declares_the_tenant_schema() -> None:
    contracts = sorted(NODES_ROOT.glob("*/contract.yaml"))
    assert contracts, f"no node contracts under {NODES_ROOT}"
    offenders = {
        str(path.relative_to(REPO_ROOT)): found
        for path in contracts
        if (found := _tenant_schema_paths(yaml.safe_load(path.read_text())))
    }
    assert offenders == {}, (
        f"OMN-17887 retired the `tenant` schema; declare `schema: public`: {offenders}"
    )


def test_ownership_manifest_declares_no_tenant_schema() -> None:
    document = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    assert _tenant_schema_paths(document) == []


def test_inventory_classifies_public_as_tenant_and_refuses_tenant() -> None:
    generator = _load_generator()
    assert generator.DOMAIN_BY_SCHEMA["public"] == "TENANT"
    assert "tenant" not in generator.DOMAIN_BY_SCHEMA


def test_checked_in_inventory_has_no_tenant_target_schema() -> None:
    payload = json.loads(INVENTORY.read_text(encoding="utf-8"))
    rows = [*payload["relations"], *payload["blocked_relations"]]
    tenant_domain = [row for row in rows if row.get("domain") == "TENANT"]
    assert tenant_domain, "positive control: the inventory has TENANT relations"
    assert [row["name"] for row in rows if row.get("target_schema") == "tenant"] == []
    assert {row["target_schema"] for row in tenant_domain} == {"public"}
