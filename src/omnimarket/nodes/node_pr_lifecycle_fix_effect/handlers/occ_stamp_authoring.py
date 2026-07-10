# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical PR OCC stamp authoring/parsing for node_pr_lifecycle_fix_effect.

Piece 3/5 of the canonical OCC stamp-model (parent epic OMN-14180, ticket
OMN-14189). Every ``Evidence-Source`` / ``Evidence-Ticket`` line this node
writes onto a product PR body or an OCC companion PR body — and every read-back
of an existing stamp — flows through the shared renderer/parser + typed models
in :mod:`omnibase_compat.contracts.pr_occ_stamp` (relocated to omnibase_compat,
the lowest zero-dep layer, so the gate and this emitter share one vocabulary
without a cross-repo cycle — OMN-14223), never a hand-built f-string or an
ad-hoc regex.

This is the single authoring/extraction seam for the node: the emitter (this
effect) and the receipt gate (``validator_receipt_gate`` / ``occ-preflight``)
share one stamp vocabulary and can never diverge on stamp shape. Ownership
ticket-set extraction (which contracts to author) stays on the gate's own
``_extract_ticket_ids`` helper — that is the gate's source of truth and is a
different concern from the ``Evidence-Ticket:`` lines rendered here.

Pure computation: zero I/O.
"""

from __future__ import annotations

from collections.abc import Sequence

from omnibase_compat.contracts.pr_occ_stamp import (
    EnumPrEvidenceSourceKind,
    ModelPrBodySection,
    ModelPrEvidenceSource,
    ModelPrOccMetadataStamp,
    parse_pr_occ_metadata_stamp,
    render_pr_occ_metadata_stamp,
)


def product_pr_occ_binding(pr_body: str) -> int | None:
    """Return the bound OCC PR number when the body already carries an OCC source.

    Reads the canonical stamp via the Piece-2 parser (not a local regex). Returns
    ``None`` when there is no ``Evidence-Source`` line, or it points at a bare
    commit SHA — the failure mode the autobind adapter repairs.
    """
    source = parse_pr_occ_metadata_stamp(pr_body).evidence_source
    if source is not None and source.kind is EnumPrEvidenceSourceKind.OCC_PR:
        return source.occ_pr_number
    return None


def product_pr_has_evidence_source(pr_body: str) -> bool:
    """True when the body carries ANY ``Evidence-Source`` line (OCC or commit SHA)."""
    return parse_pr_occ_metadata_stamp(pr_body).evidence_source is not None


def render_product_pr_body_with_occ_source(
    existing_body: str, *, occ_pr_number: int, tickets: Sequence[str]
) -> str:
    """Rebind a product PR body to ``Evidence-Source: OCC#<n>`` via the core renderer.

    Human-authored prose is preserved byte-for-byte (the parser keeps every
    non-stamp section verbatim); only the canonical Evidence block is
    re-authored. The stamped ticket set is ``tickets`` when supplied, otherwise
    whatever the body already carried — never a hand-built line.
    """
    parsed = parse_pr_occ_metadata_stamp(existing_body)
    evidence_tickets = tuple(tickets) if tickets else parsed.evidence_tickets
    rebound = parsed.model_copy(
        update={
            "evidence_source": ModelPrEvidenceSource(
                kind=EnumPrEvidenceSourceKind.OCC_PR,
                occ_pr_number=occ_pr_number,
            ),
            "evidence_tickets": evidence_tickets,
        }
    )
    return render_pr_occ_metadata_stamp(rebound)


def render_occ_companion_pr_body(prose: str, *, tickets: Sequence[str]) -> str:
    """Render an OCC companion PR body: human prose + a canonical Evidence-Ticket block.

    The companion carries no ``Evidence-Source`` of its own — the product PR is
    the surface the receipt gate reads. ``prose`` is preserved verbatim and the
    ``Evidence-Ticket`` lines are authored by the core renderer over the typed
    model.
    """
    stamp = ModelPrOccMetadataStamp(
        evidence_tickets=tuple(tickets),
        body_sections=(ModelPrBodySection(content=prose, is_stamp_section=False),),
    )
    return render_pr_occ_metadata_stamp(stamp)


__all__ = [
    "product_pr_has_evidence_source",
    "product_pr_occ_binding",
    "render_occ_companion_pr_body",
    "render_product_pr_body_with_occ_source",
]
