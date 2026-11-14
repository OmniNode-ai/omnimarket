# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure comparison of a ticket as Linear returns it with the node's own write record."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from omnimarket.nodes.node_ticket_contract_render_compute.handlers.handler_ticket_contract_render import (
    HandlerTicketContractRender,
)
from omnimarket.nodes.node_ticket_contract_render_compute.models import (
    EnumTicketContractRenderFinding,
    EnumTicketContractRenderOperation,
    EnumTicketContractRenderOutcome,
    ModelTicketContractRenderNormalization,
    ModelTicketContractRenderRequest,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models import (
    ModelDescriptionRevision,
    ModelTicketBodyReading,
    ModelTicketHandEditNormalization,
    ModelTicketWriteRecord,
)

NO_FINGERPRINT = "none"


def content_sha256(title: str, description: str) -> str:
    """The hash a written event carries: the title, a blank line, the description."""
    return hashlib.sha256(f"{title}\n\n{description}".encode()).hexdigest()


def read_body(
    description: str, normalizations: Sequence[ModelTicketHandEditNormalization]
) -> ModelTicketBodyReading:
    """Parse a description with the render node's parser: is the block there, what does it recompute to."""
    if not description.strip():
        return ModelTicketBodyReading(
            block_present=False, differences=("the description is empty",)
        )
    result = HandlerTicketContractRender().handle(
        ModelTicketContractRenderRequest(
            operation=EnumTicketContractRenderOperation.PARSE,
            body=description,
            normalizations=tuple(
                ModelTicketContractRenderNormalization(
                    pattern=n.pattern, replacement=n.replacement
                )
                for n in normalizations
            ),
        )
    )
    differences = tuple(
        f"{f.code.value}: {f.detail}" if f.detail else f.code.value
        for f in result.findings
    )
    if result.outcome is EnumTicketContractRenderOutcome.REFUSED:
        missing = [
            f.code is EnumTicketContractRenderFinding.CONTRACT_BLOCK_MISSING
            for f in result.findings
        ]
        return ModelTicketBodyReading(
            block_present=not (missing and all(missing)), differences=differences
        )
    return ModelTicketBodyReading(
        block_present=True,
        found_fingerprint=result.recomputed_fingerprint,
        differences=differences,
    )


def differences_from_record(
    found_sha256: str,
    record: ModelTicketWriteRecord | None,
    reading: ModelTicketBodyReading,
) -> tuple[str, ...]:
    """What differs; empty when the ticket needs no finding.

    With a write record, equal content is the whole answer: the node wrote exactly this. With
    none, only a ticket that holds the block and does not read back clean is a finding.
    """
    if record is not None:
        if found_sha256 == record.content_sha256:
            return ()
        found = [
            f"title or description sha256 {found_sha256} differs from the newest written event's {record.content_sha256}"
        ]
        if not reading.block_present and record.contract_fingerprint:
            found.append("the contract block was removed from a ticket the node wrote")
        elif reading.block_present:
            found.extend(reading.differences)
        return tuple(found)
    return reading.differences if reading.block_present else ()


def editors_after_write(
    revisions: Sequence[ModelDescriptionRevision],
    record: ModelTicketWriteRecord | None,
    node_actor_id: str | None,
) -> tuple[str, ...]:
    """Actors of the revisions after the node's last write, except the node's own identity."""
    return tuple(
        sorted(
            {
                actor
                for revision in revisions
                if record is None or revision.snapshot_at > record.linear_updated_at
                for actor in revision.actor_ids
                if actor != node_actor_id
            }
        )
    )


__all__ = [
    "NO_FINGERPRINT",
    "content_sha256",
    "differences_from_record",
    "editors_after_write",
    "read_body",
]
