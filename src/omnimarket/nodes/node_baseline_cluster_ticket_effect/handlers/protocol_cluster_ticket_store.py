# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The protocol the node's ticket effect needs of a ticket tracker: four synchronous calls, one request and result model each."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_baseline_cluster_ticket_effect.models.model_cluster_ticket_store import (
    ModelClusterTicketCommentRequest,
    ModelClusterTicketCommentResult,
    ModelClusterTicketCreateRequest,
    ModelClusterTicketCreateResult,
    ModelClusterTicketLookupRequest,
    ModelClusterTicketLookupResult,
    ModelClusterTicketSearchRequest,
    ModelClusterTicketSearchResult,
)


@runtime_checkable
class ProtocolClusterTicketStore(Protocol):
    """Search by the cluster's signature needles, read one ticket, create one, comment on one."""

    def search(
        self, request: ModelClusterTicketSearchRequest
    ) -> ModelClusterTicketSearchResult:
        """The tickets whose description carries any of the needles, or the failure of the search."""
        ...

    def read(
        self, request: ModelClusterTicketLookupRequest
    ) -> ModelClusterTicketLookupResult:
        """One ticket's status, description and duplicate link, or its absence, or the failure of the read."""
        ...

    def create(
        self, request: ModelClusterTicketCreateRequest
    ) -> ModelClusterTicketCreateResult:
        """One new ticket under a parent, the gate's refusal, or the failure of the create."""
        ...

    def comment(
        self, request: ModelClusterTicketCommentRequest
    ) -> ModelClusterTicketCommentResult:
        """The id of the appended comment, or the failure of the comment."""
        ...
