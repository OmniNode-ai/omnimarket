# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The eleven canonical row types of the rolling work ledger (OMN-19513).

The set is the canonical vocabulary of ``ledger-row-grammar/2`` (the grammar
``onex-ledger --print-grammar`` prints). A row of any other type is not
emittable: the parser refuses it rather than guessing a mapping.

One topic per type, one family, named in the emit registry
(``registries/topics.yaml``, events ``work.ledger.<type>``). The topic is READ
from that registry, never spelled here, so a type cannot exist without a
registered topic and the registry stays the one authority.
"""

from __future__ import annotations

from enum import StrEnum

from omnimarket.nodes.node_event_emit_effect.spool.topic_resolver import (
    resolve_event_type,
)

EVENT_TYPE_PREFIX = "work.ledger."


class EnumLedgerRowType(StrEnum):
    """A canonical ledger row type, spelled exactly as in the type cell."""

    CLAIM = "CLAIM"
    STATUS = "STATUS"
    TERMINAL = "TERMINAL"
    HOLD = "HOLD"
    RELEASE = "RELEASE"
    MSG = "MSG"
    ACK = "ACK"
    RULING = "RULING"
    OPERATOR_CONSENT = "OPERATOR-CONSENT"
    FRICTION = "FRICTION"
    CORRECTION = "CORRECTION"

    @property
    def slug(self) -> str:
        return self.value.lower()

    @property
    def topic(self) -> str:
        """The duty_critical event topic the registry declares for this row type."""
        (resolved,) = resolve_event_type(self.event_type)
        return resolved.topic

    @property
    def event_type(self) -> str:
        """The emit-registry ``event_type`` key for this row type."""
        return f"{EVENT_TYPE_PREFIX}{self.slug.replace('-', '_')}"


__all__: list[str] = [
    "EVENT_TYPE_PREFIX",
    "EnumLedgerRowType",
]
