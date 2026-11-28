# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26474 AC2: no event payload of this node carries a field named or valued like a key or a digest."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from omnimarket.nodes.node_tenant_api_key_effect.models import (
    EnumTenantApiKeyRegisterOutcome,
    EnumTenantApiKeyRevokeOutcome,
    ModelTenantApiKeyRegisterResult,
    ModelTenantApiKeyRevokeResult,
)
from tests.nodes.node_tenant_api_key_effect.support import (
    digest,
    in_hours,
    register_command,
    register_handler,
)
from tests.nodes.scratch_identity_postgres import add_tenant, scratch_identity_database

pytestmark = pytest.mark.unit

REGISTER_RESULT_KEYS = {
    "correlation_id",
    "tenant_id",
    "outcome",
    "credential_id",
    "created_at",
    "expires_at",
}
REVOKE_RESULT_KEYS = {"correlation_id", "tenant_id", "credential_id", "outcome"}
KEY_SHAPED_NAME = re.compile(
    r"(key|hash|digest|secret|token|plain|password|credential_value)", re.I
)
SRC = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_tenant_api_key_effect"
)


def test_payload_key_sets_exact_for_the_declared_result_models() -> None:
    assert set(ModelTenantApiKeyRegisterResult.model_fields) == REGISTER_RESULT_KEYS
    assert set(ModelTenantApiKeyRevokeResult.model_fields) == REVOKE_RESULT_KEYS
    for name in REGISTER_RESULT_KEYS | REVOKE_RESULT_KEYS:
        assert KEY_SHAPED_NAME.search(name) is None, name


async def test_payload_key_sets_exact_for_every_register_payload_a_handler_produces() -> (
    None
):
    async with scratch_identity_database() as dsn:
        tenant = await add_tenant(dsn)
        handler = register_handler(dsn)
        shapes: list[dict[str, object]] = []
        try:
            for command in (
                register_command(tenant, expires_at=in_hours(1)),
                register_command(tenant, expires_at=in_hours(1)),
                register_command(
                    tenant, name="other", key_hash=digest(f"{tenant}/laptop")
                ),
                register_command(tenant, name="Bad Name"),
                register_command(tenant, name="short", key_hash="abc"),
            ):
                shapes.append((await handler.handle(command)).model_dump(mode="json"))
        finally:
            await handler.close()
    assert len(shapes) == 5
    for payload in shapes:
        assert set(payload) == REGISTER_RESULT_KEYS
        for value in payload.values():
            assert not re.fullmatch(r"[0-9a-f]{64}", str(value))
            assert not str(value).startswith("onxk")
    assert digest(f"{tenant}/laptop") not in repr(shapes)


def test_payload_key_sets_exact_outcomes_are_typed_words_not_values() -> None:
    for outcome in (*EnumTenantApiKeyRegisterOutcome, *EnumTenantApiKeyRevokeOutcome):
        assert re.fullmatch(r"[a-z_]+", outcome.value)


def test_payload_key_sets_exact_the_source_names_no_key_literal() -> None:
    needle = re.compile("plaintext|onxk" + "_")
    hits = [
        f"{path.relative_to(SRC)}:{number}"
        for path in SRC.rglob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if needle.search(line)
    ]
    assert hits == []
