# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GET /v1/tenants/me/inference-credentials through the mounted app.

Read degrades, authorization does not: a missing projection relation is an empty
list, an unset source is a 503, and every other failure is a 500.
"""

from __future__ import annotations

import pytest

from omnimarket.handlers.inference_credentials_http.errors import (
    CredentialReadModelMissingTableError,
    CredentialReadModelNotConfiguredError,
    CredentialReadModelUnavailableError,
)
from tests.nodes.inference_credential_http_support import (
    ROUTE,
    TENANT_A,
    TENANT_B,
    World,
    fake_row,
)

pytestmark = pytest.mark.unit


class TestList:
    def test_list_returns_only_the_callers_rows(self) -> None:
        rows = [
            fake_row(TENANT_A, "abc"),
            fake_row(TENANT_A, "def", revoked=True),
            fake_row(TENANT_B, "zzz"),
        ]
        world = World(rows=rows)
        response = world.client.get(ROUTE, headers=world.as_tenant_a())
        assert response.status_code == 200, response.text
        body = response.json()
        assert [item["api_key_ref"] for item in body] == [
            rows[0]["api_key_ref"],
            rows[1]["api_key_ref"],
        ]
        assert all("key_value" not in item and "value" not in item for item in body)
        assert all("tenant_id" not in item for item in body)
        assert body[0]["revoked_at"] is None
        assert body[1]["revoked_at"] is not None
        assert world.read_model.calls == [("list", str(TENANT_A))]

    def test_two_tenants_never_see_each_other(self) -> None:
        world = World(rows=[fake_row(TENANT_A, "abc"), fake_row(TENANT_B, "zzz")])
        a = world.client.get(ROUTE, headers=world.as_tenant_a()).json()
        b = world.client.get(ROUTE, headers=world.as_tenant_b()).json()
        assert [i["api_key_ref"] for i in a] == [f"cred_{TENANT_A}_openai_abc"]
        assert [i["api_key_ref"] for i in b] == [f"cred_{TENANT_B}_openai_zzz"]

    def test_list_empty_returns_empty_list(self) -> None:
        world = World()
        response = world.client.get(ROUTE, headers=world.as_tenant_a())
        assert response.status_code == 200
        assert response.json() == []


class TestDegradeWhenBackingRelationMissing:
    def test_missing_relation_returns_200_empty_list(self) -> None:
        world = World()
        world.read_model.error = CredentialReadModelMissingTableError("absent")
        response = world.client.get(ROUTE, headers=world.as_tenant_a())
        assert response.status_code == 200, response.text
        assert response.json() == []

    def test_degraded_response_is_byte_equal_to_a_real_empty_list(self) -> None:
        world = World()
        genuine = world.client.get(ROUTE, headers=world.as_tenant_a())
        world.read_model.error = CredentialReadModelMissingTableError("absent")
        degraded = world.client.get(ROUTE, headers=world.as_tenant_a())
        assert degraded.status_code == genuine.status_code == 200
        assert degraded.content == genuine.content

    def test_an_unset_source_is_a_503_not_an_empty_list(self) -> None:
        world = World()
        world.read_model.error = CredentialReadModelNotConfiguredError("unset")
        response = world.client.get(ROUTE, headers=world.as_tenant_a())
        assert response.status_code == 503, response.text
        assert response.json() == {"detail": "failed to list inference credentials"}

    @pytest.mark.parametrize(
        "error",
        [
            CredentialReadModelUnavailableError(
                "analytics DB error (OperationalError)"
            ),
            CredentialReadModelUnavailableError(
                "analytics DB error (InsufficientPrivilege)"
            ),
            RuntimeError("database error (UndefinedTable)"),
        ],
    )
    def test_every_other_failure_is_a_500(self, error: Exception) -> None:
        world = World()
        world.read_model.error = error
        response = world.client.get(ROUTE, headers=world.as_tenant_a())
        assert response.status_code == 500, response.text
        assert response.json()["detail"] == "failed to list inference credentials"
