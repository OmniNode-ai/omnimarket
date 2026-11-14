# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain for the dev signed-in read node (OMN-24345): every refusal is typed, in order, and the guard comes first.

A lane or realm the contract does not declare is refused before the store is read or any request is sent. After the guard
each step fails into exactly one outcome: a missing password, a refused grant, no response from Keycloak, no response from
the API, a 401 or 403, any other non-200, and a 200 whose body has no declared data state.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnimarket.nodes.node_dev_signed_in_read_effect.handlers import (
    ModelDevSignedInReadCommand,
)
from omnimarket.nodes.node_dev_signed_in_read_effect.models import (
    EnumDevSignedInReadOutcome as Outcome,
)

from ._support import build, overlay_document, seeded_store
from .fakes import FakeKeycloakAndApi, FakeSettingStore

pytestmark = pytest.mark.unit


def _run(
    transport: FakeKeycloakAndApi,
    store: FakeSettingStore | None = None,
    *,
    lane: str = "dev",
    document: dict[str, Any] | None = None,
) -> Any:
    handler = build(transport, store or seeded_store(), document)
    return asyncio.run(
        handler.handle(ModelDevSignedInReadCommand(lane=lane, write=True))
    )


def test_command_lane_outside_the_contract_is_refused_before_any_call() -> None:
    transport = FakeKeycloakAndApi()
    store = seeded_store()
    result = _run(transport, store, lane="stability")
    assert result.outcome is Outcome.REFUSED_NOT_DEV
    assert result.ok is False
    assert transport.sent == []
    assert store.calls == []


def test_overlay_lane_outside_the_contract_is_refused_before_any_call() -> None:
    document = overlay_document()
    document["lane"] = "stability"
    transport = FakeKeycloakAndApi()
    store = seeded_store()
    result = _run(transport, store, document=document)
    assert result.outcome is Outcome.REFUSED_NOT_DEV
    assert transport.sent == []
    assert store.calls == []


def test_realm_outside_the_contract_is_refused_before_any_call() -> None:
    document = overlay_document()
    document["keycloak"]["realm"] = "master"
    transport = FakeKeycloakAndApi()
    store = seeded_store()
    result = _run(transport, store, document=document)
    assert result.outcome is Outcome.REFUSED_NOT_DEV
    assert transport.sent == []
    assert store.calls == []


def test_missing_password_is_typed_and_names_where_it_looked() -> None:
    transport = FakeKeycloakAndApi()
    result = _run(transport, FakeSettingStore())
    assert result.outcome is Outcome.PASSWORD_MISSING
    assert result.expected_secret == "/shared/env/DEV_PROBE_USER_PASSWORD"
    assert transport.sent == []


def test_refused_password_grant_is_typed_and_no_read_is_made() -> None:
    transport = FakeKeycloakAndApi(fail_user_token=True)
    result = _run(transport)
    assert result.outcome is Outcome.USER_TOKEN_REFUSED
    assert [r.method for r in transport.sent] == ["POST"]


def test_no_answer_from_keycloak_is_typed() -> None:
    transport = FakeKeycloakAndApi(
        raise_connection_error_on="/protocol/openid-connect/token"
    )
    result = _run(transport)
    assert result.outcome is Outcome.KEYCLOAK_UNREACHABLE
    assert "ConnectionError" in result.detail


def test_no_answer_from_the_api_is_typed() -> None:
    transport = FakeKeycloakAndApi(raise_connection_error_on="/v1/projections")
    result = _run(transport)
    assert result.outcome is Outcome.API_UNREACHABLE


@pytest.mark.parametrize("status", [401, 403])
def test_unauthorized_read_is_typed_with_its_status(status: int) -> None:
    result = _run(FakeKeycloakAndApi(read_status=status))
    assert result.outcome is Outcome.READ_UNAUTHORIZED
    assert result.http_status == status
    assert result.ok is False


def test_a_bearer_the_route_does_not_accept_is_unauthorized() -> None:
    result = _run(FakeKeycloakAndApi(issued_token="a-token-the-route-rejects"))
    assert result.outcome is Outcome.READ_UNAUTHORIZED
    assert result.http_status == 401


@pytest.mark.parametrize("status", [404, 422, 500, 503])
def test_other_non_200_is_a_refused_read(status: int) -> None:
    result = _run(FakeKeycloakAndApi(read_status=status))
    assert result.outcome is Outcome.READ_REFUSED
    assert result.http_status == status


@pytest.mark.parametrize(
    "body",
    [None, [], {"data_state": "unknown_state"}, {"data_state": 3}, {"rows": []}],
)
def test_a_200_without_a_declared_data_state_is_invalid(body: Any) -> None:
    result = _run(FakeKeycloakAndApi(read_body=body))
    assert result.outcome is Outcome.READ_RESPONSE_INVALID
    assert result.http_status == 200
    assert result.ok is False
