# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real-backend cluster ticket store binding: the creator's mapping, the build.

The creator is tested through the ``creator_factory`` seam and a stand-in for the one
ticket node's handler; the store is built over resolvers that answer fixtures, so no
transport touches a host and no secret is read. The search, the read and the comment
run inside the store over the one poster the build hands it; nothing here reaches
Linear on a second path.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from omnimarket.config.service_endpoints import service_endpoint_url
from omnimarket.handlers import (
    baseline_cluster_ticket_linear_store as container_linear,
)
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
from omnimarket.nodes.node_baseline_cluster_ticket_effect.handlers.protocol_cluster_ticket_store import (
    ProtocolClusterTicketStore,
)
from omnimarket.nodes.node_baseline_cluster_ticket_effect.models.model_cluster_ticket_store import (
    ModelClusterTicketCreateRequest,
    ModelClusterTicketCreateResult,
)
from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    TicketGuardRefusedError,
    TicketGuardUnavailableError,
)
from omnimarket.nodes.node_floor_task_ticket_effect.handlers.http_transport_linear_graphql import (
    LinearGraphqlPoster,
)

pytestmark = pytest.mark.unit

KEY = "linear-key-fixture"
URL = service_endpoint_url("linear.graphql_url")
TIMEOUT = 30.0

CLUSTER_REQUEST = ModelClusterTicketCreateRequest(
    title="[dev-baseline] Test failure cluster: tests.nightly (omnibase_infra)",
    description="Cluster signature: dev-baseline:omnibase_infra:tests.nightly\n",
    parent="OMN-19998",
)


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


# ------------------------------------------------------------------ the creator


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
    result = store.create(CLUSTER_REQUEST)
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
            CLUSTER_REQUEST.title,
            CLUSTER_REQUEST.description,
            CLUSTER_REQUEST.parent,
        )
    else:
        assert (result.state, result.cause, result.ticket) == (state, detail, None)


# ------------------------------------------------------------------ the build


def test_build_linear_store_calls_the_key_resolver_once_and_passes_the_lane() -> None:
    resolved: list[str] = []

    def resolve_key() -> str:
        resolved.append(KEY)
        return KEY

    lanes: list[str] = []

    def factory(
        lane: str,
    ) -> Callable[[ModelClusterTicketCreateRequest], ModelClusterTicketCreateResult]:
        lanes.append(lane)
        return lambda _: ModelClusterTicketCreateResult(
            state="created", ticket="OMN-42"
        )

    store = container_linear.build_linear_store(
        "lane-x",
        api_key_resolver=resolve_key,
        url_resolver=lambda: URL,
        creator_factory=factory,
        timeout_seconds=TIMEOUT,
    )
    assert resolved == [KEY]
    assert lanes == ["lane-x"]
    assert isinstance(store, ProtocolClusterTicketStore)
    assert store.create(CLUSTER_REQUEST) == ModelClusterTicketCreateResult(
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
