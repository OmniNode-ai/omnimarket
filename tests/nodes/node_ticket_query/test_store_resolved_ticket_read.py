# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lab-fill reads a ticket through node_ticket_query with the key from the store (OMN-22418).

The Linear key is resolved in process by node_secret_store_effect. These tests
fail if any step of the read needs ``LINEAR_API_KEY`` (or ``LINEAR_TOKEN``) in
the environment, and pass when only the store holds it.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from omnimarket.models.secret_store.model_secret_store_request import (
    ModelSecretStoreRequest,
)
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
    ModelSecretStoreResult,
)
from omnimarket.models.ticket_query.model_ticket_credential_refusal import (
    TicketQueryCredentialRefusedError,
)
from omnimarket.models.ticket_query.model_ticket_facts import (
    ModelTicketFactsInput,
)
from omnimarket.nodes.node_lab_fill_effect.protocols import LabFillPortError
from omnimarket.nodes.node_lab_fill_effect.protocols import (
    local_lab_fill_adapters as adapters,
)
from omnimarket.nodes.node_lab_fill_effect.protocols.local_lab_fill_adapters import (
    LocalLiveChecks,
    NodeTicketFactsReader,
)
from omnimarket.nodes.node_ticket_query.handlers.handler_ticket_facts import (
    HandlerTicketFacts,
)
from omnimarket.nodes.node_ticket_query.handlers.store_resolved_tracker import (
    linear_secret_reference,
    resolve_linear_tracker,
)

_STORE_KEY = "store-held-linear-key"


class _FakeSecretNode:
    """Stands in for node_secret_store_effect's handler: one resolve per call."""

    def __init__(self, outcome: EnumSecretStoreOutcome, value: str | None) -> None:
        self._outcome = outcome
        self._value = value
        self.requests: list[ModelSecretStoreRequest] = []

    async def handle(self, request: ModelSecretStoreRequest) -> ModelSecretStoreResult:
        self.requests.append(request)
        return ModelSecretStoreResult(
            operation=request.operation,
            outcome=self._outcome,
            value=SecretStr(self._value) if self._value is not None else None,
        )


class _FakeTracker:
    """Records the key it was built with and answers one raw GraphQL read."""

    def __init__(self, api_key: str, answer: dict[str, object]) -> None:
        self.api_key = api_key
        self._answer = answer
        self.queries: list[str] = []

    async def post_graphql(
        self, query: str, variables: dict[str, object] | None = None
    ) -> httpx.Response:
        self.queries.append(query)
        return httpx.Response(200, json=self._answer)


def _no_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.delenv("LINEAR_TOKEN", raising=False)


def _issue_answer(state_type: str, assignee_id: str | None) -> dict[str, object]:
    node: dict[str, object] = {
        "identifier": "OMN-1",
        "state": {"type": state_type},
        "assignee": {"id": assignee_id} if assignee_id else None,
    }
    return {"data": {"issues": {"nodes": [node]}}}


@pytest.mark.asyncio
async def test_key_comes_from_the_store_with_no_environment_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_env_key(monkeypatch)
    secrets = _FakeSecretNode(EnumSecretStoreOutcome.RESOLVED, _STORE_KEY)
    built: list[_FakeTracker] = []

    def factory(api_key: str) -> _FakeTracker:
        built.append(_FakeTracker(api_key, _issue_answer("started", "me")))
        return built[-1]

    handler = HandlerTicketFacts(
        tracker_provider=lambda: resolve_linear_tracker(secrets, factory)
    )
    out = await handler.handle(ModelTicketFactsInput(identifier="OMN-1"))
    assert (out.state_type, out.assignee_id) == ("started", "me")
    assert built[0].api_key == _STORE_KEY
    assert secrets.requests[0].reference == linear_secret_reference()


@pytest.mark.asyncio
async def test_missing_store_entry_is_refused_naming_the_reference_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An environment key is present; it must never be used in the store's place.
    monkeypatch.setenv("LINEAR_API_KEY", "env-key-that-must-not-be-used")
    secrets = _FakeSecretNode(EnumSecretStoreOutcome.NOT_FOUND, None)
    with pytest.raises(TicketQueryCredentialRefusedError) as raised:
        await resolve_linear_tracker(secrets, lambda key: _FakeTracker(key, {}))
    assert linear_secret_reference() in str(raised.value)
    assert raised.value.secret_reference == linear_secret_reference()
    assert raised.value.outcome == "not_found"
    assert "env-key-that-must-not-be-used" not in str(raised.value)


@pytest.mark.asyncio
async def test_unconfigured_store_is_a_typed_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_env_key(monkeypatch)
    secrets = _FakeSecretNode(EnumSecretStoreOutcome.NOT_CONFIGURED, None)
    with pytest.raises(TicketQueryCredentialRefusedError, match="not_configured"):
        await resolve_linear_tracker(secrets, lambda key: _FakeTracker(key, {}))


@pytest.mark.asyncio
async def test_facts_for_an_unknown_ticket_is_named() -> None:
    secrets = _FakeSecretNode(EnumSecretStoreOutcome.RESOLVED, _STORE_KEY)
    handler = HandlerTicketFacts(
        tracker_provider=lambda: resolve_linear_tracker(
            secrets, lambda key: _FakeTracker(key, {"data": {"issues": {"nodes": []}}})
        )
    )
    with pytest.raises(LookupError, match="OMN-7"):
        await handler.handle(ModelTicketFactsInput(identifier="OMN-7"))


def test_lab_fill_skip_reason_reads_through_the_node_with_no_environment_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_env_key(monkeypatch)
    monkeypatch.setattr(LocalLiveChecks, "_fences", staticmethod(lambda _ledger: set()))
    secrets = _FakeSecretNode(EnumSecretStoreOutcome.RESOLVED, _STORE_KEY)
    reader = NodeTicketFactsReader(
        secret_node=secrets,
        tracker_factory=lambda key: _FakeTracker(key, _issue_answer("started", "me")),
    )
    checks = LocalLiveChecks(facts=reader)
    kwargs = {"pr": "", "operator_id": "me", "ledger_path": "/l"}
    assert checks.skip_reason(ticket="OMN-1", kind="ticket", **kwargs) == ("", "")
    other = {"pr": "", "operator_id": "someone-else", "ledger_path": "/l"}
    assert checks.skip_reason(ticket="OMN-1", kind="ticket", **other)[0] == (
        "assigned-other"
    )


def test_lab_fill_refuses_with_the_secret_reference_when_the_store_has_no_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "env-key-that-must-not-be-used")
    secrets = _FakeSecretNode(EnumSecretStoreOutcome.NOT_FOUND, None)
    reader = NodeTicketFactsReader(
        secret_node=secrets, tracker_factory=lambda key: _FakeTracker(key, {})
    )
    with pytest.raises(LabFillPortError, match="LINEAR_API_KEY") as raised:
        LocalLiveChecks(facts=reader).skip_reason(
            ticket="OMN-1",
            kind="ticket",
            pr="",
            operator_id="me",
            ledger_path="/l",
        )
    assert linear_secret_reference() in str(raised.value)


def test_no_lab_fill_code_path_reads_the_key_from_the_environment() -> None:
    source = Path(adapters.__file__).read_text(encoding="utf-8")
    assert 'os.environ.get("LINEAR' not in source
    assert "_graphql" not in source
    assert "urllib.request" not in source


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "", "   "])
async def test_resolved_store_without_a_usable_key_is_refused_before_adapter_creation(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "env-key-that-must-not-be-used")
    secrets = _FakeSecretNode(EnumSecretStoreOutcome.RESOLVED, value)
    built: list[str] = []

    def factory(api_key: str) -> _FakeTracker:
        built.append(api_key)
        return _FakeTracker(api_key, {})

    with pytest.raises(TicketQueryCredentialRefusedError) as raised:
        await resolve_linear_tracker(secrets, factory)
    assert raised.value.outcome == "value_missing"
    assert raised.value.secret_reference == linear_secret_reference()
    assert "env-key-that-must-not-be-used" not in str(raised.value)
    assert built == []
