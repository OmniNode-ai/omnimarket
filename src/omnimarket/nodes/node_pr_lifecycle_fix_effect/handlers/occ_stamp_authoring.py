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

# OMN-18853: the stamp COUNT and the per-line OCC numbers are read with the
# receipt gate's own canonical-region stripper and line patterns, imported and
# never re-derived, so this producer and the gate can never disagree about
# whether a body carries one evidence-source line or several.
from omnibase_core.validation.validator_receipt_gate import (
    EVIDENCE_SOURCE_LINE_PATTERN,
    EVIDENCE_SOURCE_OCC_PR_PATTERN,
    strip_noncanonical_regions,
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


def product_pr_evidence_source_line_count(pr_body: str) -> int:
    """How many canonical evidence-source lines the receipt gate would count.

    The gate refuses a body carrying more than one (OMN-14410), after blanking
    fenced and quoted regions (OMN-14682). This is that exact count, so a
    duplicate is detected here on the same terms the gate fails it on.
    """
    canonical = strip_noncanonical_regions(pr_body)
    return len(list(EVIDENCE_SOURCE_LINE_PATTERN.finditer(canonical)))


def product_pr_occ_stamp_numbers(pr_body: str) -> tuple[int, ...]:
    """Every OCC PR number named by a canonical evidence-source line, in order.

    Distinct numbers only, first occurrence wins. A line in the commit-SHA form
    names no companion and is not listed.
    """
    canonical = strip_noncanonical_regions(pr_body)
    seen: list[int] = []
    for match in EVIDENCE_SOURCE_OCC_PR_PATTERN.finditer(canonical):
        number = int(match.group(1))
        if number not in seen:
            seen.append(number)
    return tuple(seen)


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
    "product_pr_evidence_source_line_count",
    "product_pr_has_evidence_source",
    "product_pr_occ_binding",
    "product_pr_occ_stamp_numbers",
    "render_occ_companion_pr_body",
    "render_product_pr_body_with_occ_source",
]
