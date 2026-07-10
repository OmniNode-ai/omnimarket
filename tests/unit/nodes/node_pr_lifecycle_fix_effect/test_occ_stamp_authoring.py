# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the OCC stamp authoring seam (OMN-14189, Piece 3/5).

These lock the property that matters for the epic: every Evidence-Source /
Evidence-Ticket line this node writes is authored by the Piece-2 core renderer
over the Piece-1 typed models and round-trips cleanly back through the Piece-2
core parser — no inline f-string authoring and no local regex extraction path.
"""

from __future__ import annotations

import pytest
from omnibase_compat.contracts.pr_occ_stamp import (
    EnumPrEvidenceSourceKind,
    parse_pr_occ_metadata_stamp,
)

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_stamp_authoring import (
    product_pr_has_evidence_source,
    product_pr_occ_binding,
    render_occ_companion_pr_body,
    render_product_pr_body_with_occ_source,
)


@pytest.mark.unit
class TestProductPrOccBinding:
    def test_returns_occ_number_when_bound(self) -> None:
        body = "prose\n\nEvidence-Ticket: OMN-1\nEvidence-Source: OCC#2801\n"
        assert product_pr_occ_binding(body) == 2801

    def test_returns_none_for_bare_sha_source(self) -> None:
        # A product-head SHA source is exactly the failure mode autobind repairs.
        body = "prose\n\nEvidence-Source: 040eb235abcdef\n"
        assert product_pr_occ_binding(body) is None

    def test_returns_none_when_no_evidence_source(self) -> None:
        assert product_pr_occ_binding("just a body, no stamp") is None


@pytest.mark.unit
class TestProductPrHasEvidenceSource:
    def test_true_for_occ_source(self) -> None:
        assert product_pr_has_evidence_source("x\nEvidence-Source: OCC#5\n") is True

    def test_true_for_sha_source(self) -> None:
        assert product_pr_has_evidence_source("x\nEvidence-Source: deadbeef1\n") is True

    def test_false_when_absent(self) -> None:
        assert product_pr_has_evidence_source("no stamp here") is False


@pytest.mark.unit
class TestRenderProductPrBodyRoundTrips:
    def test_rebinds_sha_source_to_occ_and_round_trips(self) -> None:
        existing = "Fixes a thing.\n\nEvidence-Source: 040eb235abcdef\n"

        rendered = render_product_pr_body_with_occ_source(
            existing, occ_pr_number=2801, tickets=["OMN-9999", "OMN-1234"]
        )

        # The rendered body parses back cleanly through the CORE parser — proof
        # the authoring path is the renderer, not a hand-built string.
        stamp = parse_pr_occ_metadata_stamp(rendered)
        assert stamp.evidence_source is not None
        assert stamp.evidence_source.kind is EnumPrEvidenceSourceKind.OCC_PR
        assert stamp.evidence_source.occ_pr_number == 2801
        assert list(stamp.evidence_tickets) == ["OMN-9999", "OMN-1234"]
        # Human prose preserved verbatim; the stale product-SHA source is gone.
        assert "Fixes a thing." in rendered
        assert "040eb235abcdef" not in rendered

    def test_render_is_idempotent(self) -> None:
        existing = "Body.\n\nEvidence-Source: 040eb235abcdef\n"
        once = render_product_pr_body_with_occ_source(
            existing, occ_pr_number=42, tickets=["OMN-1"]
        )
        twice = render_product_pr_body_with_occ_source(
            once, occ_pr_number=42, tickets=["OMN-1"]
        )
        assert once == twice

    def test_falls_back_to_existing_tickets_when_none_supplied(self) -> None:
        existing = "Body.\n\nEvidence-Ticket: OMN-7\nEvidence-Source: aaaaaaa\n"
        rendered = render_product_pr_body_with_occ_source(
            existing, occ_pr_number=9, tickets=[]
        )
        stamp = parse_pr_occ_metadata_stamp(rendered)
        assert list(stamp.evidence_tickets) == ["OMN-7"]
        assert stamp.evidence_source is not None
        assert stamp.evidence_source.occ_pr_number == 9


@pytest.mark.unit
class TestRenderOccCompanionBody:
    def test_appends_canonical_evidence_ticket_block(self) -> None:
        prose = "Autobind OCC evidence for `OMN-9999`.\n\nTriggered by X.\n"
        rendered = render_occ_companion_pr_body(prose, tickets=["OMN-9999"])

        stamp = parse_pr_occ_metadata_stamp(rendered)
        assert list(stamp.evidence_tickets) == ["OMN-9999"]
        # The companion carries no Evidence-Source of its own.
        assert stamp.evidence_source is None
        assert "Autobind OCC evidence" in rendered
        assert "Evidence-Ticket: OMN-9999" in rendered
        # No fabricated `auto-contract-<run_id>` self-source token.
        assert "auto-contract-" not in rendered
