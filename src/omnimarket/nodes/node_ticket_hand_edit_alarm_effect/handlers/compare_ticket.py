# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compare one ticket as Linear holds it with the node's newest write of it. Pure."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from omnimarket.models.model_ticket_contract_render import (
    EnumTicketContractRenderFinding,
    EnumTicketContractRenderOperation,
    EnumTicketContractRenderOutcome,
    ModelTicketContractRenderNormalization,
    ModelTicketContractRenderRequest,
    ModelTicketContractRenderResult,
)
from omnimarket.models.model_ticket_contract_written import (
    ModelTicketContractWritten,
    ticket_content_sha256,
)
from omnimarket.nodes.node_ticket_contract_render_compute.node import (
    NodeTicketContractRenderCompute,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models import (
    ModelDescriptionRevision,
    ModelLinearIssueSnapshot,
)

_RENDER = NodeTicketContractRenderCompute()


@dataclass(frozen=True, slots=True)
class TicketComparison:
    """What differs between Linear and the write record, and what was found."""

    differs: tuple[str, ...]
    fingerprint_found: str | None
    content_sha256_found: str


def parse_body(
    description: str,
    normalizations: Sequence[ModelTicketContractRenderNormalization],
) -> ModelTicketContractRenderResult | None:
    """The render node's parse of a body; None when the body holds no contract block."""
    if not description.strip():
        return None
    parsed = _RENDER.handle(
        ModelTicketContractRenderRequest(
            operation=EnumTicketContractRenderOperation.PARSE,
            body=description,
            normalizations=tuple(normalizations),
        )
    )
    missing = EnumTicketContractRenderFinding.CONTRACT_BLOCK_MISSING
    if parsed.outcome is EnumTicketContractRenderOutcome.REFUSED and any(
        finding.code is missing for finding in parsed.findings
    ):
        return None
    return parsed


def compare_ticket(
    issue: ModelLinearIssueSnapshot,
    written: ModelTicketContractWritten | None,
    normalizations: Sequence[ModelTicketContractRenderNormalization],
) -> TicketComparison:
    """Every difference between the issue and the newest write; empty when they agree."""
    found_hash = ticket_content_sha256(issue.title, issue.description)
    differs: list[str] = []
    if written is not None and written.content_sha256 != found_hash:
        differs.append(
            f"title and description hash {found_hash[:16]}, the node wrote {written.content_sha256[:16]}"
        )
    fingerprint_found: str | None = None
    parsed = parse_body(issue.description, normalizations)
    if parsed is None:
        if written is not None and written.contract_fingerprint:
            differs.append("the contract block the node wrote was removed")
        return TicketComparison(tuple(differs), None, found_hash)
    if parsed.outcome is EnumTicketContractRenderOutcome.PARSED:
        fingerprint_found = parsed.recomputed_fingerprint or None
    if written is not None and written.content_sha256 == found_hash:
        # The node wrote or adopted exactly this text: what it holds is the node's.
        return TicketComparison((), fingerprint_found, found_hash)
    for finding in parsed.findings:
        if finding.code is EnumTicketContractRenderFinding.CONTRACT_NOT_SYNCED:
            differs.append(
                f"the block's fingerprint does not recompute: {finding.detail}"
            )
        elif finding.code is EnumTicketContractRenderFinding.RENDERED_TEXT_DRIFT:
            differs.append(f"the text differs from a fresh render: {finding.detail}")
        else:
            differs.append(f"{finding.code.value}: {finding.detail}")
    if (
        written is not None
        and written.contract_fingerprint
        and fingerprint_found is not None
        and fingerprint_found != written.contract_fingerprint
    ):
        differs.append(
            f"the block's fingerprint {fingerprint_found}, the node wrote {written.contract_fingerprint}"
        )
    return TicketComparison(tuple(differs), fingerprint_found, found_hash)


def editors_after(
    revisions: Sequence[ModelDescriptionRevision],
    written: ModelTicketContractWritten | None,
) -> tuple[str, ...]:
    """Actor ids of the revisions after the node's last write, in first-seen order."""
    actors: list[str] = []
    for revision in sorted(revisions, key=lambda r: r.snapshot_at):
        if written is not None and revision.snapshot_at <= written.updated_at:
            continue
        for actor in revision.actor_ids:
            if actor not in actors:
                actors.append(actor)
    return tuple(actors)


__all__ = ["TicketComparison", "compare_ticket", "editors_after", "parse_body"]
