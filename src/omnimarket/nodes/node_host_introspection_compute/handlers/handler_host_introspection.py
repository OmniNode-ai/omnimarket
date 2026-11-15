# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerHostIntrospection -- one host's announcement from its facts and its capacity (OMN-24367).

Canonical def-B handler: ``handle(request: ModelHostIntrospectionRequest) -> ModelHostAnnouncement``. Pure:
the digest is the sha256 of the facts in a canonical form (lists sorted, keys sorted), so the same facts in a
different reading order give the same digest, and a changed version, commit, runtime, card, model, account,
role or availability gives a different one. The capacity is not hashed and is carried unchanged: load moves it
every beat, and a digest that moved with load would announce "changed" every ten seconds.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from omnimarket.nodes.node_host_introspection_compute.models.model_host_announcement import (
    ModelHostAnnouncement,
    ModelHostFacts,
    ModelHostIntrospectionRequest,
)


def _canonical(value: object) -> object:
    """Order every list by its own canonical JSON so reading order never changes the digest."""
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        items = [_canonical(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
    return value


def facts_digest(facts: ModelHostFacts) -> str:
    """The sha256 hex digest of the host's facts, independent of load and of reading order."""
    body = json.dumps(
        _canonical(facts.model_dump(mode="json")), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class HandlerHostIntrospection:
    """COMPUTE handler: facts and capacity in, announcement out."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    def handle(self, request: ModelHostIntrospectionRequest) -> ModelHostAnnouncement:
        return ModelHostAnnouncement(
            host_name=request.facts.host_name,
            reason=request.reason,
            announced_at=request.announced_at,
            facts_digest=facts_digest(request.facts),
            facts=request.facts,
            capacity=request.capacity,
        )


__all__ = ["HandlerHostIntrospection", "facts_digest"]
