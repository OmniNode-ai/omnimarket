# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-18238 — a proposed binding reaches the consumer marked as a proposal.

The closer discharges an acceptance criterion when a verified check declares it.
That is the right rule for a binding a person wrote, and the wrong rule the
moment a machine can write one: the cheap way to make bindings plentiful is to
let an autobinder guess a criterion from a matching check name, and a passing
check with a matching name is not proof of the criterion it names.

So a machine may PROPOSE and a person decides. This module pins the two halves
that live in this repository.

**The verifier surfaces which claims are proposals.** It is the only place the
distinction can be reported from: the closer runs on a runner with no contract
checkout, and a second contract parser would be a second truth that drifts from
this one. The draft labels are carried ALONGSIDE the claim rather than removed
from it, because "claimed but not yet accepted" and "not claimed at all" are
different facts and the consumer's hold reason has to tell them apart.

**The emitter cannot propose an acceptance.** `render_draft_ac_binding` takes no
actor and no timestamp, so no argument produces an acceptance record. That is
the enforcement: not a rule saying the generator should only propose, but a
renderer with no way to say anything else.

RED before the change: `ModelEvidenceCheckResult` had no `draft_binds_ac` field,
so every assertion below raised `AttributeError`, and a proposal was
indistinguishable from a binding by the time it reached the closer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    render_draft_ac_binding,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

_HASH = "9d006777c2e97aabd867d0c48c23fac73e0608770843ba66a24fcd093b73080f"


def _contract(tmp_path: Path, items: list[dict[str, object]]) -> str:
    path = tmp_path / "OMN-9999.yaml"
    path.write_text(
        yaml.safe_dump({"ticket_id": "OMN-9999", "dod_evidence": items}),
        encoding="utf-8",
    )
    return str(path)


def _item(bindings: list[dict[str, object]] | None) -> dict[str, object]:
    item: dict[str, object] = {
        "id": "dod-tests",
        "description": "proves the criteria it claims",
        "binds_ac": ["AC1", "AC2"],
        "checks": [{"check_type": "command", "check_value": "true"}],
    }
    if bindings is not None:
        item["ac_bindings"] = bindings
    return item


# ------------------------------------------------------- the verifier half ---


class TestDraftLabelsAreSurfaced:
    def test_a_record_with_no_acceptance_is_reported_as_a_draft(
        self, tmp_path: Path
    ) -> None:
        path = _contract(
            tmp_path,
            [_item([{"label": "AC1", "criterion_hash": _HASH}])],
        )

        results = EvidenceCollector().collect("OMN-9999", contract_path=path)

        assert results
        assert all(result.draft_binds_ac == ("AC1",) for result in results)
        # The CLAIM is untouched. "Claimed but not accepted" and "not claimed"
        # are different facts, and the consumer needs both.
        assert all(result.binds_ac == ("AC1", "AC2") for result in results)

    def test_an_accepted_record_is_not_a_draft(self, tmp_path: Path) -> None:
        path = _contract(
            tmp_path,
            [
                _item(
                    [
                        {
                            "label": "AC1",
                            "criterion_hash": _HASH,
                            "accepted_by": "an-approving-reviewer",
                            "accepted_at": "2026-09-12T21:00:00Z",
                        }
                    ]
                )
            ],
        )

        results = EvidenceCollector().collect("OMN-9999", contract_path=path)

        assert results
        assert all(result.draft_binds_ac == () for result in results)

    def test_a_contract_with_no_records_reports_no_drafts(self, tmp_path: Path) -> None:
        """The corpus default, and it must stay silent.

        Sixty-three live contracts declare a claim and carry no record. Every
        one was hand-authored, which IS the acceptance the rule asks for.
        """
        path = _contract(tmp_path, [_item(None)])

        results = EvidenceCollector().collect("OMN-9999", contract_path=path)

        assert results
        assert all(result.draft_binds_ac == () for result in results)
        assert all(result.binds_ac == ("AC1", "AC2") for result in results)

    def test_a_draft_for_an_unclaimed_criterion_is_ignored(
        self, tmp_path: Path
    ) -> None:
        """The narrowing rule: a record can DEMOTE a claim, never add one."""
        path = _contract(
            tmp_path,
            [_item([{"label": "AC9", "criterion_hash": _HASH}])],
        )

        results = EvidenceCollector().collect("OMN-9999", contract_path=path)

        assert results
        assert all(result.draft_binds_ac == () for result in results)

    def test_the_join_survives_a_different_spelling_on_each_side(
        self, tmp_path: Path
    ) -> None:
        path = _contract(
            tmp_path,
            [_item([{"label": "ac-1", "criterion_hash": _HASH}])],
        )

        results = EvidenceCollector().collect("OMN-9999", contract_path=path)

        assert results
        # Reported under the label the CLAIM spells, so the consumer subtracts
        # like from like.
        assert all(result.draft_binds_ac == ("AC1",) for result in results)

    def test_an_unreadable_record_is_treated_as_a_draft(self, tmp_path: Path) -> None:
        """Fail toward holding. The failure being avoided is a proposal counted
        as proof, so anything unreadable resolves to the side that holds."""
        path = _contract(tmp_path, [_item(["not-a-mapping"])])

        results = EvidenceCollector().collect("OMN-9999", contract_path=path)

        assert results
        assert all(result.draft_binds_ac == () for result in results)


# -------------------------------------------------------- the emitter half ---


class TestTheEmitterCanOnlyPropose:
    def test_a_rendered_binding_carries_no_acceptance(self) -> None:
        """AC1's first clause, enforced by the signature rather than by a rule.

        There is no argument to this function that produces an acceptance.
        """
        rendered = render_draft_ac_binding(
            label="AC1", criterion_hash=_HASH, proposed_by="occ-autobind"
        )

        assert "accepted_by" not in rendered
        assert "accepted_at" not in rendered
        assert "AC1" in rendered
        assert _HASH in rendered
        assert "occ-autobind" in rendered

    def test_it_records_what_proposed_the_binding(self) -> None:
        """A reviewer accepting it should see they are accepting a machine's
        reading rather than their own."""
        rendered = render_draft_ac_binding(
            label="AC1", criterion_hash=_HASH, proposed_by="occ-autobind"
        )

        assert 'proposed_by: "occ-autobind"' in rendered

    def test_the_rendered_fragment_parses_as_the_record_shape(self) -> None:
        rendered = render_draft_ac_binding(
            label="AC1", criterion_hash=_HASH, proposed_by="occ-autobind"
        )
        parsed = yaml.safe_load("ac_bindings:\n" + rendered)

        assert parsed == {
            "ac_bindings": [
                {
                    "label": "AC1",
                    "criterion_hash": _HASH,
                    "proposed_by": "occ-autobind",
                }
            ]
        }

    @pytest.mark.parametrize(
        ("label", "criterion_hash", "proposed_by"),
        [
            ("", _HASH, "occ-autobind"),
            ("AC1", _HASH, ""),
            ("AC1", "abc123", "occ-autobind"),
            ("AC1", "Z" * 64, "occ-autobind"),
            ("AC1", "", "occ-autobind"),
        ],
    )
    def test_a_proposal_pinned_to_nothing_is_refused(
        self, label: str, criterion_hash: str, proposed_by: str
    ) -> None:
        """A record the consumer reads as a binding whose criterion it cannot
        check is worse than no record."""
        with pytest.raises(ValueError, match="proposed binding"):
            render_draft_ac_binding(
                label=label, criterion_hash=criterion_hash, proposed_by=proposed_by
            )

    def test_an_uppercase_hash_is_normalised_rather_than_refused(self) -> None:
        rendered = render_draft_ac_binding(
            label="AC1", criterion_hash=_HASH.upper(), proposed_by="occ-autobind"
        )

        assert _HASH in rendered
