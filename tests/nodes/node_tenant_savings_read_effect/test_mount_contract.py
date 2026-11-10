# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Both reads are contract-declared mounts with the narrowest prefix and no overlap."""

from __future__ import annotations

import pytest
from omnibase_infra.models.model_http_mount import ModelHttpMount

from tests.nodes.tenant_read_support import contract_of

pytestmark = pytest.mark.unit
NODES = {
    "node_tenant_savings_read_effect": "/v1/tenants/me/savings",
    "node_tenant_delegations_read_effect": "/v1/tenants/me/delegations",
}


@pytest.mark.parametrize(("node", "path"), NODES.items())
def test_each_route_has_its_own_contract_mount_equal_to_its_path(
    node: str, path: str
) -> None:
    contract = contract_of(node)
    mount = ModelHttpMount.from_contract_block(contract["http_mount"])
    assert mount is not None
    assert mount.path_prefix == path
    assert contract["http_route"]["path"] == path
    assert contract["http_route"]["methods"] == ["GET"]
    assert mount.factory.startswith(f"omnimarket.nodes.{node}.handlers.http_app:")


def _prefix(node: str) -> str:
    mount = ModelHttpMount.from_contract_block(contract_of(node)["http_mount"])
    assert mount is not None
    return mount.path_prefix


def test_the_mounts_never_cover_the_private_siblings_or_each_other() -> None:
    prefixes = [_prefix(n) for n in NODES]
    broad = {"/v1/tenants", "/v1/tenants/me", "/v1/platform", "/v1/platform/savings"}
    assert not set(prefixes) & broad
    for a in prefixes:
        for b in prefixes:
            if a != b:
                assert not b.startswith(a + "/")


@pytest.mark.parametrize("node", NODES)
def test_secrets_are_named_logically_and_no_environment_is_read(node: str) -> None:
    from pathlib import Path

    contract = contract_of(node)
    assert contract["config"]["analytics_dsn_secret"] == "omnidash.analytics.db_url"
    assert any(d["name"] == "secret_resolver" for d in contract["dependencies"])
    root = Path(__file__).resolve().parents[3] / "src/omnimarket"
    for source in (
        *(root / "nodes" / node).rglob("*.py"),
        *(root / "handlers/tenant_projection_read").rglob("*.py"),
        root / "handlers/container/container_tenant_read_dependencies.py",
    ):
        text = source.read_text()
        assert "os.environ" not in text, source
        assert "getenv" not in text, source
