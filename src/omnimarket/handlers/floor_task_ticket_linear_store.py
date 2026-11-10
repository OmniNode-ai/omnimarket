# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real-backend binding site of node_floor_task_ticket_effect: its ticket store on Linear.

The reader and the key are resolved when the store is built (a missing key fails there,
with the resolver's original exception, and the caller types it); create goes through a
creator the caller can take over. It lives beside the shared handlers, not in the node, because
the node imports no other node: this module is the one place that imports ``node_create_ticket`` for it. The cause of a create that did not pass keeps the gate's codes
and nothing else: the text of an exception may carry credentials, so a failed create
names its type, never its message.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from omnibase_spi.contracts.services import (
    ModelFloorTicketCreateRequest,
    ModelFloorTicketCreateResult,
)
from omnibase_spi.protocols.lane import ProtocolFloorTicketStore

from omnimarket.config.service_endpoints import service_endpoint_url
from omnimarket.handlers.handler_lane_intake import LaneIntakeRefusedError
from omnimarket.inference.secret_store_resolver import SecretStoreConfigurationError
from omnimarket.models.model_ticket_io import (
    EnumTicketOperation,
    ModelCreateTicketRequest,
)
from omnimarket.models.secret_store.model_secret_resolution_error import (
    SecretResolutionError,
)
from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    HandlerCreateTicket,
    TicketGuardRefusedError,
    TicketGuardUnavailableError,
    linear_api_key,
)
from omnimarket.nodes.node_floor_task_ticket_effect.adapters.adapter_graphql_floor_ticket_store import (
    GraphqlFloorTicketStore,
)
from omnimarket.nodes.node_floor_task_ticket_effect.adapters.adapter_linear_graphql_poster import (
    LinearGraphqlPoster,
)

LINEAR_GRAPHQL_URL_REF = "linear.graphql_url"


@dataclass(frozen=True)
class _LinearCreator:
    """Create through the one ticket node, answering the store's three states."""

    lane: str

    def __call__(
        self, request: ModelFloorTicketCreateRequest
    ) -> ModelFloorTicketCreateResult:
        handler = HandlerCreateTicket(lane_id=self.lane, require_description_stamp=True)
        try:
            result = handler.handle(
                ModelCreateTicketRequest(
                    operation=EnumTicketOperation.CREATE,
                    title=request.title,
                    description=request.description,
                    parent=request.parent or None,
                )
            )
        except TicketGuardRefusedError as error:
            codes = ",".join(error.finding_codes)
            return ModelFloorTicketCreateResult(
                state="refused", cause=codes or "guard-refused"
            )
        except (TicketGuardUnavailableError, LaneIntakeRefusedError):
            return ModelFloorTicketCreateResult(
                state="failed", cause="guard-unavailable"
            )
        except (SecretResolutionError, SecretStoreConfigurationError):
            return ModelFloorTicketCreateResult(
                state="failed", cause="secret-unavailable"
            )
        except Exception as error:
            # The text of a create failure may name headers or keys; the type alone stands.
            return ModelFloorTicketCreateResult(
                state="failed", cause=type(error).__name__
            )
        if result.status == "created" and result.ticket_id:
            return ModelFloorTicketCreateResult(
                state="created", ticket=result.ticket_id
            )
        return ModelFloorTicketCreateResult(
            state="refused",
            cause=",".join(result.finding_codes) or "invalid-request",
        )


def build_linear_store(
    lane: str,
    *,
    api_key_resolver: Callable[[], str] | None = None,
    url_resolver: Callable[[], str] | None = None,
    creator_factory: Callable[[str], Any] | None = None,
    timeout_seconds: float,
) -> ProtocolFloorTicketStore:
    """The Linear ticket store: the reader over the contract's GraphQL, create through the one ticket node.

    ``timeout_seconds`` takes the overlay's ``floor_fill.mint_timeout_seconds``; there is no
    default here. The key is resolved now (a missing key fails here, with the resolver's
    original exception), so the caller types it.
    """
    if not timeout_seconds > 0:
        raise ValueError("timeout_seconds must be a float > 0")
    resolve_key = api_key_resolver if api_key_resolver is not None else linear_api_key
    resolve_url = (
        url_resolver
        if url_resolver is not None
        else lambda: service_endpoint_url(LINEAR_GRAPHQL_URL_REF)
    )
    poster = LinearGraphqlPoster(
        url=resolve_url(),
        api_key=resolve_key(),
        timeout_seconds=timeout_seconds,
    )
    create = (creator_factory or (lambda built_lane: _LinearCreator(built_lane)))(lane)
    return GraphqlFloorTicketStore(graphql=poster, creator=create)


__all__: list[str] = ["build_linear_store"]
