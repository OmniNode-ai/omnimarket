# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Effect handler: publish judged-acceptance events through the local emit daemon.

The path delegation terminals use: ``omnimarket.events.emit_client`` over the daemon's Unix
socket, never a direct database or broker write. ``EVENT_TYPE`` is the key the daemon's event
registry must map to the judged-acceptance topic declared in this node's contract; a daemon that has
no such registration refuses the event, and the run fails. A socket that is absent, or that refuses
an event, fails the run and names the socket; it never reports success.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Protocol

from omnimarket.events.emit_client import EmitClient, default_socket_path
from omnimarket.models.delegation_acceptance_judge.enum_acceptance_publish_status import (
    EnumAcceptancePublishStatus,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_publish_request import (
    ModelAcceptancePublishRequest,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_publish_result import (
    ModelAcceptancePublishResult,
)
from omnimarket.models.delegation_acceptance_judge.model_delegation_acceptance_judged_event import (
    ModelDelegationAcceptanceJudgedEvent,
)

# The emit daemon registry key; its fan-out topic must be the contract's judged topic.
EVENT_TYPE = "delegation.acceptance_judged"


class ProtocolEmitClient(Protocol):
    def emit_sync(self, event_type: str, payload: dict[str, object]) -> str: ...

    def close(self) -> None: ...


class HandlerDelegationAcceptanceJudgedPublish:
    """Send each judged-acceptance event to the emit daemon, in order, stopping at the first refusal."""

    def __init__(
        self, *, client_factory: Callable[[str], ProtocolEmitClient] | None = None
    ) -> None:
        self._client_factory = client_factory or (lambda path: EmitClient(path))

    @staticmethod
    def publish_event(
        client: ProtocolEmitClient, event: ModelDelegationAcceptanceJudgedEvent
    ) -> None:
        """Send one event to the daemon; the daemon's refusal raises."""
        client.emit_sync(EVENT_TYPE, event.model_dump(mode="json"))

    def handle(
        self, request: ModelAcceptancePublishRequest
    ) -> ModelAcceptancePublishResult:
        path = request.socket_path or default_socket_path()
        requested = len(request.events)
        if not os.path.exists(path):
            return ModelAcceptancePublishResult(
                status=EnumAcceptancePublishStatus.FAILED,
                socket_path=path,
                requested_count=requested,
                published_count=0,
                reason=(
                    f"emit socket {path} does not exist; start the emit daemon or set "
                    f"ONEX_EMIT_SOCKET_PATH. Nothing was published."
                ),
            )
        client = self._client_factory(path)
        published = 0
        try:
            for event in request.events:
                self.publish_event(client, event)
                published += 1
        except (
            OSError,
            ValueError,
        ) as exc:  # effect boundary: socket and daemon refusals
            return ModelAcceptancePublishResult(
                status=EnumAcceptancePublishStatus.FAILED,
                socket_path=path,
                requested_count=requested,
                published_count=published,
                reason=(
                    f"emit socket {path} refused event {published + 1} of {requested}: "
                    f"{exc!r}. {published} published."
                ),
            )
        finally:
            client.close()
        return ModelAcceptancePublishResult(
            status=EnumAcceptancePublishStatus.COMPLETED,
            socket_path=path,
            requested_count=requested,
            published_count=published,
        )
