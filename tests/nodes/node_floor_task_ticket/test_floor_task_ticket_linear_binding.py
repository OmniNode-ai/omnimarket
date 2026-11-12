# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real-backend ticket store binding: the poster's transport, the creator's mapping, the build.

The poster is tested over an injected opener (a scripted ``urlopen`` stand-in), so no transport
touches a host; the creator is tested through the ``creator_factory`` seam and a stand-in for the
one ticket node's handler; the store is run end to end under ``HandlerFloorTaskTicketMint``.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

import pytest
from omnibase_spi.contracts.services import (
    ModelFloorTicketCreateRequest,
    ModelFloorTicketCreateResult,
)
from omnibase_spi.protocols.lane import ProtocolFloorTicketStore

from omnimarket.config.service_endpoints import service_endpoint_url
from omnimarket.handlers import floor_task_ticket_linear_store as container_linear
from omnimarket.handlers.handler_lane_intake import LaneIntakeRefusedError
from omnimarket.inference.secret_store_resolver import SecretStoreConfigurationError
from omnimarket.models.model_ticket_io import (
    EnumTicketOperation,
    ModelCreateTicketRequest,
    ModelCreateTicketResult,
)
from omnimarket.models.secret_store.model_secret_resolution_error import (
    SecretResolutionError,
)
from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    TicketGuardRefusedError,
    TicketGuardUnavailableError,
)
from omnimarket.nodes.node_floor_task_ticket_effect.adapters.adapter_graphql_floor_ticket_store import (
    READ_QUERY,
    GraphqlFloorTicketStore,
)
from omnimarket.nodes.node_floor_task_ticket_effect.handlers import (
    HandlerFloorTaskTicketMint,
)
from omnimarket.nodes.node_floor_task_ticket_effect.handlers.http_transport_linear_graphql import (
    LinearGraphqlAnswerError,
    LinearGraphqlPoster,
)
from omnimarket.nodes.node_floor_task_ticket_effect.models import (
    ModelFloorTaskTicketMintRequest,
)

pytestmark = pytest.mark.unit

KEY = "linear-key-fixture"
URL = service_endpoint_url("linear.graphql_url")
TIMEOUT = 30.0

FLOOR_REQUEST = ModelFloorTicketCreateRequest(
    floor_key="floor:a:T1",
    title="Run the floor task",
    description="Floor key: floor:a:T1",
    parent="OMN-1",
)


class _FakeResponse:
    """The scripted answer's response: a body and a status."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        return None


class _ScriptedOpener:
    """A stand-in for ``urllib.request.urlopen``: one scripted answer per call."""

    def __init__(self, *answers: Mapping[str, Any] | bytes | tuple[bytes, int]) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[urllib.request.Request, float]] = []

    def __call__(
        self, request: urllib.request.Request, timeout: float
    ) -> _FakeResponse:
        self.calls.append((request, timeout))
        answer = self.answers.pop(0)
        if isinstance(answer, Mapping):
            return _FakeResponse(json.dumps(answer).encode("utf-8"))
        if isinstance(answer, tuple):
            return _FakeResponse(answer[0], answer[1])
        return _FakeResponse(answer)


def _poster(opener: _ScriptedOpener, key: str = KEY) -> LinearGraphqlPoster:
    return LinearGraphqlPoster(
        url=URL, api_key=key, timeout_seconds=TIMEOUT, opener=opener
    )


# ------------------------------------------------------------------ the poster


def test_the_poster_posts_the_document_with_the_bare_key_to_the_contract_url() -> None:
    opener = _ScriptedOpener({"data": {"issue": None}})
    assert _poster(opener).post(READ_QUERY, {"id": "OMN-1"}) == {
        "data": {"issue": None}
    }
    (request, timeout) = opener.calls[0]
    assert request.full_url == URL
    assert request.get_method() == "POST"
    assert json.loads(request.data) == {
        "query": READ_QUERY,
        "variables": {"id": "OMN-1"},
    }
    # urllib normalizes the header names of a Request with ``str.capitalize``.
    headers = dict(request.header_items())
    assert headers["Content-type"] == "application/json"
    assert headers["Authorization"] == KEY
    assert not headers["Authorization"].startswith("Bearer ")
    assert timeout == TIMEOUT


def test_a_poster_answer_that_carries_graphql_errors_is_returned_not_raised() -> None:
    opener = _ScriptedOpener({"data": None, "errors": [{"message": "unauthorized"}]})
    assert _poster(opener).post(READ_QUERY, {})["errors"] == [
        {"message": "unauthorized"}
    ]


def test_a_poster_raises_on_a_non_2xx_answer_without_echoing_the_key() -> None:
    opener = _ScriptedOpener((b"{}", 500))
    with pytest.raises(LinearGraphqlAnswerError) as caught:
        _poster(opener).post(READ_QUERY, {})
    assert KEY not in str(caught.value)
    assert KEY not in repr(caught.value)


def test_a_poster_raises_on_an_undecodable_answer() -> None:
    with pytest.raises(LinearGraphqlAnswerError):
        _poster(_ScriptedOpener(b"not json")).post(READ_QUERY, {})


def test_the_poster_never_echoes_the_key() -> None:
    poster = _poster(_ScriptedOpener({}))
    assert KEY not in repr(poster)
    assert KEY not in str(poster)


# ------------------------------------------------------------------ the creator


def _created(ticket: str = "OMN-9") -> ModelCreateTicketResult:
    return ModelCreateTicketResult(status="created", ticket_id=ticket)


def _invalid(codes: list[str] | None = None) -> ModelCreateTicketResult:
    return ModelCreateTicketResult(status="error", finding_codes=codes or [])


class _FakeHandler:
    """The stand-in handler of the one ticket node: it returns or raises its outcome."""

    def __init__(self, outcome: Any) -> None:
        self._outcome = outcome
        self.requests: list[ModelCreateTicketRequest] = []
        self.lane_id: str | None = None
        self.require_description_stamp: bool = False

    def handle(self, request: ModelCreateTicketRequest) -> ModelCreateTicketResult:
        self.requests.append(request)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class _FakeHandlerClass:
    """Answers ``HandlerCreateTicket(lane_id=..., require_description_stamp=...)``."""

    def __init__(self, outcome: Any) -> None:
        self.instances: list[_FakeHandler] = []
        self._outcome = outcome

    def __call__(
        self, lane_id: str | None = None, require_description_stamp: bool = False
    ) -> _FakeHandler:
        handler = _FakeHandler(self._outcome)
        handler.lane_id = lane_id
        handler.require_description_stamp = require_description_stamp
        self.instances.append(handler)
        return handler


@pytest.mark.parametrize(
    ("outcome", "state", "detail"),
    [
        (_created(), "created", "OMN-9"),
        (
            TicketGuardRefusedError(
                "refused", ["lane_stamp_missing", "missing_gate_line"]
            ),
            "refused",
            "lane_stamp_missing,missing_gate_line",
        ),
        (TicketGuardRefusedError("refused"), "refused", "guard-refused"),
        (_invalid(["invalid_parent"]), "refused", "invalid_parent"),
        (_invalid(), "refused", "invalid-request"),
        (TicketGuardUnavailableError("gone"), "failed", "guard-unavailable"),
        (LaneIntakeRefusedError("no lane"), "failed", "guard-unavailable"),
        (SecretResolutionError("secret text"), "failed", "secret-unavailable"),
        (
            SecretStoreConfigurationError("bad configuration"),
            "failed",
            "secret-unavailable",
        ),
        # The text of the failure may name headers or keys; the type alone stands.
        (RuntimeError("text-with-secret"), "failed", "RuntimeError"),
    ],
)
def test_the_creator_maps_the_outcomes_of_the_one_ticket_node(
    monkeypatch: pytest.MonkeyPatch, outcome: Any, state: str, detail: str
) -> None:
    factory = _FakeHandlerClass(outcome)
    monkeypatch.setattr(container_linear, "HandlerCreateTicket", factory)
    store = container_linear.build_linear_store(
        "lane-x",
        api_key_resolver=lambda: KEY,
        url_resolver=lambda: URL,
        timeout_seconds=TIMEOUT,
    )
    result = store.create(FLOOR_REQUEST)
    if state == "created":
        assert (result.state, result.ticket, result.cause) == ("created", detail, None)
        [handler] = factory.instances
        assert (handler.lane_id, handler.require_description_stamp) == ("lane-x", True)
        [request] = handler.requests
        assert (
            request.operation,
            request.title,
            request.description,
            request.parent,
        ) == (
            EnumTicketOperation.CREATE,
            FLOOR_REQUEST.title,
            FLOOR_REQUEST.description,
            FLOOR_REQUEST.parent,
        )
    else:
        assert (result.state, result.cause, result.ticket) == (state, detail, None)


def test_build_linear_store_calls_the_key_resolver_once_and_passes_the_lane() -> None:
    resolved: list[str] = []

    def resolve_key() -> str:
        resolved.append(KEY)
        return KEY

    lanes: list[str] = []

    def factory(
        lane: str,
    ) -> Callable[[ModelFloorTicketCreateRequest], ModelFloorTicketCreateResult]:
        lanes.append(lane)
        return lambda _: ModelFloorTicketCreateResult(state="created", ticket="OMN-42")

    store = container_linear.build_linear_store(
        "lane-x",
        api_key_resolver=resolve_key,
        url_resolver=lambda: URL,
        creator_factory=factory,
        timeout_seconds=TIMEOUT,
    )
    assert resolved == [KEY]
    assert lanes == ["lane-x"]
    assert isinstance(store, ProtocolFloorTicketStore)
    assert store.create(FLOOR_REQUEST) == ModelFloorTicketCreateResult(
        state="created", ticket="OMN-42"
    )


def test_build_linear_store_defaults_the_url_to_the_contract_reference() -> None:
    store = container_linear.build_linear_store(
        "lane-x",
        api_key_resolver=lambda: KEY,
        timeout_seconds=TIMEOUT,
    )
    poster = store.graphql
    assert isinstance(poster, LinearGraphqlPoster)
    assert poster.url == service_endpoint_url("linear.graphql_url")
    assert poster.api_key == KEY
    assert poster.timeout_seconds == TIMEOUT


def test_a_key_that_cannot_be_resolved_fails_at_build_time() -> None:
    def failing_resolver() -> str:
        raise SecretResolutionError("secret text")

    with pytest.raises(SecretResolutionError, match="secret text"):
        container_linear.build_linear_store(
            "lane-x",
            api_key_resolver=failing_resolver,
            url_resolver=lambda: URL,
            timeout_seconds=TIMEOUT,
        )


def test_build_linear_store_refuses_a_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="timeout_seconds must be a float > 0"):
        container_linear.build_linear_store(
            "lane-x",
            api_key_resolver=lambda: KEY,
            url_resolver=lambda: URL,
            timeout_seconds=0.0,
        )


# ------------------------------------------------------------------ end to end


def _readback_issue() -> dict[str, Any]:
    return {
        "identifier": "OMN-42",
        "state": {"name": "Backlog", "type": "backlog"},
        "description": "Floor key: floor:a:T1",
        "project": None,
        "labels": {"nodes": []},
        "children": {"nodes": []},
    }


def _search_miss() -> dict[str, Any]:
    return {
        "data": {
            "issues": {
                "nodes": [],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        }
    }


def _created_creator() -> Callable[
    [ModelFloorTicketCreateRequest], ModelFloorTicketCreateResult
]:
    return lambda _: ModelFloorTicketCreateResult(state="created", ticket="OMN-42")


def _mint_request() -> ModelFloorTaskTicketMintRequest:
    return ModelFloorTaskTicketMintRequest(
        floor_key="floor:a:T1",
        title="Run the floor task",
        description="Floor key: floor:a:T1",
        parent="OMN-1",
        mint_timeout_seconds=TIMEOUT,
    )


def _store_over(opener: _ScriptedOpener) -> GraphqlFloorTicketStore:
    return GraphqlFloorTicketStore(graphql=_poster(opener), creator=_created_creator())


def test_a_store_over_a_scripted_poster_mints_a_ticket_end_to_end() -> None:
    opener = _ScriptedOpener(_search_miss(), {"data": {"issue": _readback_issue()}})
    result = HandlerFloorTaskTicketMint(_store_over(opener)).handle(_mint_request())
    assert result.state == "ticket"
    assert result.ticket == "OMN-42"
    assert result.reused is False
    # The search missed and the read-back found the ticket, both over the poster.
    assert [request.full_url for request, _ in opener.calls] == [URL, URL]
    assert [timeout for _, timeout in opener.calls] == [TIMEOUT, TIMEOUT]


def test_a_readback_that_carries_graphql_errors_ends_the_mint_unavailable() -> None:
    readback = {
        "data": {"issue": _readback_issue()},
        "errors": [{"message": "project resolver"}],
    }
    opener = _ScriptedOpener(_search_miss(), readback)
    result = HandlerFloorTaskTicketMint(_store_over(opener)).handle(_mint_request())
    assert result.state == "unavailable"
    assert result.cause == "readback-unreadable"
    assert result.ticket is None
