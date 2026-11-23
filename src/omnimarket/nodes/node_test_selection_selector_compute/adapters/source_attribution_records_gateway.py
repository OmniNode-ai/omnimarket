# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The published nightly attribution records, read through the repository gateway node."""

from __future__ import annotations

from collections.abc import Sequence

from omnimarket.handlers.scm.handler_gateway_read_client import HandlerGatewayReadClient
from omnimarket.models.model_gateway_node_skew import GatewayReadError
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_attribution_record_read import (
    HandlerAttributionRecordRead,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_attribution_record_reading import (
    ModelAttributionRecordReading,
    ModelAttributionRecordReadRequest,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_stage_receipt import (
    ModelReceiptNight,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_selector import (
    ModelSelectorPolicy,
)
from omnimarket.nodes.node_test_selection_selector_compute.protocols.protocol_gateway_read import (
    ProtocolGatewayRead,
)


class SourceAttributionRecordsGateway:
    """Implements ``ProtocolAttributionRecordSource`` over the gateway's ``workflow_runs`` and ``commit_check_runs``.

    Reads nothing until asked, so a tree that needs no record (stage 0) makes no call. A gateway that gives
    no answer is an unreadable source (``None``): the gate then refuses with ``records_unavailable``. The
    gateway runs in process with the hosted token (``checks:read`` and ``actions:read`` are all it needs) or
    answers over the bus on a lab lane.
    """

    def __init__(
        self,
        gateway: ProtocolGatewayRead | None = None,
        policy: ModelSelectorPolicy | None = None,
    ) -> None:
        self._gateway = gateway if gateway is not None else HandlerGatewayReadClient()
        self._policy = policy

    def read_records(
        self, repository: str, nights: Sequence[ModelReceiptNight]
    ) -> ModelAttributionRecordReading | None:
        handler = HandlerAttributionRecordRead(self._gateway, self._policy)
        try:
            return handler.handle(
                ModelAttributionRecordReadRequest(
                    repository=repository, nights=tuple(nights)
                )
            )
        except GatewayReadError:
            return None
