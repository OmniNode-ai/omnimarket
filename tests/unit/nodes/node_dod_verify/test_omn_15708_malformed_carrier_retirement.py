# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-15708 — a malformed-marker carrier must still be retirable by supersession.

``EvidenceCollector._collect_impl`` phase 3 (OMN-15382/OMN-15390) evaluated, per
item::

    1. malformed_reason = supersession.malformed.get(index)   # own marker OK?
       if malformed_reason is not None: FAILED; continue      # unconditional
    2. carrier_index = in_effect.get(item_id_str)              # am I superseded?

Because (1) ran before (2), an item whose OWN ``evidence_artifact`` marker is
malformed (e.g. a comma-joined ``supersedes_dod_evidence:<a>,<b>,<c>`` — the
parser resolves the whole string as a single, nonexistent target id, so it
reads DANGLING_SUPERSESSION) hard-FAILED even when a LATER, separate,
well-formed, single-id marker validly targeted and retired IT. No append-only
repair could ever clear the FAIL, because appending a correctly-shaped
superseding entry never touches ``supersession.malformed[index]`` for the
original malformed carrier — the branch that would retire it was unreachable.

Live instance (OMN-15374): ``onex_change_control`` PR #6080 added
``occ6077-receipt-attribution-fix`` with evidence_artifact
``"supersedes_dod_evidence:occ-self-bind-pr-5855-superseded,dod-15374-apply-role-unchanged-superseded,occ-self-bind-pr-6077"``
(comma-joined, malformed). PR #6084 then added three correctly-formed,
single-target ``supersedes_dod_evidence:occ6077-receipt-attribution-fix``
entries, each individually verified. ``dod_verify`` on OMN-15374 still capped
at 22/23 with the sole permanent failure being
``occ6077-receipt-attribution-fix: DANGLING_SUPERSESSION`` — a structurally
unreachable repair.

Fix: reorder phase 3 so "am I superseded-in-effect by a later, verified item"
is checked BEFORE "is my own marker malformed". A malformed marker on an item
that is itself validly retired no longer matters — the item is SUPERSEDED and
its own defective repair attempt is moot. A malformed marker on an item that
is NOT superseded still hard-FAILs exactly as before (AC2) — the parser itself
is untouched: a comma-joined marker never resolves to more than one target.

RED-before / GREEN-after: reverting the phase-3 ordering (malformed check
ahead of the in_effect check) turns
``TestMalformedCarrierIsStillRetirable`` RED; the parser continuing to reject
a non-superseded comma-joined marker is pinned by
``test_a_non_superseded_malformed_marker_still_hard_fails`` and would go RED
if ``_resolve_supersessions`` were changed to accept comma-joined targets.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.__main__ import _build_receipt
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    ModelDodVerifyState,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

_MARKER_PREFIX = "supersedes_dod_evidence:"


def _item(
    item_id: str,
    *,
    check_value: str | None = None,
    supersedes: str | None = None,
) -> dict[str, Any]:
    """Build one dod_evidence item. Mirrors the OMN-15390 suite's helper."""
    item: dict[str, Any] = {"id": item_id, "description": f"item {item_id}"}
    item["checks"] = (
        []
        if check_value is None
        else [{"check_type": "command", "check_value": check_value}]
    )
    if supersedes is not None:
        item["evidence_artifact"] = f"{_MARKER_PREFIX}{supersedes}"
    return item


def _write_contract(
    tmp_path: Path, dod_evidence: list[dict[str, Any]]
) -> tuple[str, Path]:
    ticket_id = f"OMN-{uuid4().int % 90000 + 10000}"
    contract_path = tmp_path / f"{ticket_id}.yaml"
    contract_path.write_text(
        yaml.dump(
            {
                "schema_version": "1.0.0",
                "ticket_id": ticket_id,
                "dod_evidence": dod_evidence,
            }
        ),
        encoding="utf-8",
    )
    return ticket_id, contract_path


def _verify(tmp_path: Path, dod_evidence: list[dict[str, Any]]) -> ModelDodVerifyState:
    """Load + execute a real contract through the real collector and handler."""
    ticket_id, contract_path = _write_contract(tmp_path, dod_evidence)
    results = EvidenceCollector().collect(ticket_id, contract_path=str(contract_path))
    return HandlerDodVerify()._handle_typed(
        ModelDodVerifyStartCommand(
            ticket_id=ticket_id,
            contract_path=str(contract_path),
        ),
        evidence_results=results,
    )


def _by_id(state: ModelDodVerifyState) -> dict[str, EnumEvidenceCheckStatus]:
    return {check.evidence_id: check.status for check in state.checks}


@pytest.mark.unit
class TestMalformedCarrierIsStillRetirable:
    def test_omn_15374_shape_comma_joined_carrier_retired_by_a_valid_single_id_marker(
        self, tmp_path: Path
    ) -> None:
        """AC3: the exact OMN-15374/OCC#6080/#6084 shape retires cleanly.

        ``dod-carrier`` (the OCC#6080 shape) declares a comma-joined marker
        naming three targets in one string — malformed, resolves to a single
        nonexistent id (DANGLING_SUPERSESSION under the pre-fix code). A LATER
        item, ``dod-repair`` (the OCC#6084 shape), declares a well-formed,
        single-id marker targeting ``dod-carrier`` and verifies. Before the
        fix: ``dod-carrier`` is FAILED regardless. After the fix:
        ``dod-carrier`` is SUPERSEDED, ``dod-repair`` VERIFIED, 0 failures.
        """
        state = _verify(
            tmp_path,
            [
                _item(
                    "dod-carrier",
                    check_value="true",
                    supersedes="dod-x-superseded,dod-y-superseded,dod-z-superseded",
                ),
                _item("dod-repair", check_value="true", supersedes="dod-carrier"),
            ],
        )
        statuses = _by_id(state)

        assert statuses["dod-carrier"] == EnumEvidenceCheckStatus.SUPERSEDED
        assert statuses["dod-repair"] == EnumEvidenceCheckStatus.VERIFIED
        assert state.failed_count == 0
        assert state.superseded_count == 1
        assert state.status == EnumDodVerifyStatus.VERIFIED
        assert _build_receipt(state, None, tmp_path)["status"] == "PASS"

    def test_omn_15374_shape_with_three_separate_single_id_repairs(
        self, tmp_path: Path
    ) -> None:
        """The literal OCC#6084 shape: three independent single-target repairs.

        Only one needs to verify for the carrier to be retired (the terminal
        superseder is whichever later item actually proves the retirement) —
        here all three verify, mirroring the live PR #6084 contract exactly.
        """
        state = _verify(
            tmp_path,
            [
                _item(
                    "occ6077-receipt-attribution-fix",
                    check_value="true",
                    supersedes=(
                        "occ-self-bind-pr-5855-superseded,"
                        "dod-15374-apply-role-unchanged-superseded,"
                        "occ-self-bind-pr-6077"
                    ),
                ),
                _item(
                    "attribution-fix-repair-1",
                    check_value="true",
                    supersedes="occ6077-receipt-attribution-fix",
                ),
                _item(
                    "attribution-fix-repair-2",
                    check_value="true",
                    supersedes="occ6077-receipt-attribution-fix",
                ),
                _item(
                    "attribution-fix-repair-3",
                    check_value="true",
                    supersedes="occ6077-receipt-attribution-fix",
                ),
            ],
        )
        statuses = _by_id(state)

        assert (
            statuses["occ6077-receipt-attribution-fix"]
            == EnumEvidenceCheckStatus.SUPERSEDED
        )
        assert state.failed_count == 0
        assert state.status == EnumDodVerifyStatus.VERIFIED

    def test_a_non_superseded_malformed_marker_still_hard_fails(
        self, tmp_path: Path
    ) -> None:
        """AC2: the same comma-joined marker, with NO valid supersession
        targeting the carrier, still fails DANGLING_SUPERSESSION exactly as
        before. Malformed parsing behavior for the non-superseded case is
        unchanged — only the carrier's OWN retirement path changed.
        """
        state = _verify(
            tmp_path,
            [
                _item(
                    "dod-carrier",
                    check_value="true",
                    supersedes="dod-x-superseded,dod-y-superseded,dod-z-superseded",
                ),
            ],
        )
        statuses = _by_id(state)

        assert statuses["dod-carrier"] == EnumEvidenceCheckStatus.FAILED
        assert state.failed_count == 1
        assert state.status == EnumDodVerifyStatus.FAILED
        rejected = next(c for c in state.checks if c.evidence_id == "dod-carrier")
        assert "DANGLING_SUPERSESSION" in rejected.message
        assert _build_receipt(state, None, tmp_path)["status"] == "FAIL"

    def test_a_malformed_carrier_superseded_by_an_unverified_repair_still_fails(
        self, tmp_path: Path
    ) -> None:
        """Anti-laundering still holds for a malformed carrier.

        A repair item that targets the malformed carrier but does not itself
        verify (checks: [] -> SKIPPED) does not retire anything — same
        ``_supersession_is_in_effect`` rule as every other supersession. The
        malformed carrier then falls through to its own (still malformed,
        still FAILED) verdict.
        """
        state = _verify(
            tmp_path,
            [
                _item(
                    "dod-carrier",
                    check_value="true",
                    supersedes="dod-x-superseded,dod-y-superseded",
                ),
                _item("dod-repair", supersedes="dod-carrier"),  # checks: [] -> SKIPPED
            ],
        )
        statuses = _by_id(state)

        assert statuses["dod-carrier"] == EnumEvidenceCheckStatus.FAILED
        assert statuses["dod-repair"] == EnumEvidenceCheckStatus.SKIPPED
        assert state.superseded_count == 0
        assert state.status == EnumDodVerifyStatus.FAILED
        rejected = next(c for c in state.checks if c.evidence_id == "dod-carrier")
        assert "DANGLING_SUPERSESSION" in rejected.message
