# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain test for node_review_finding_identity_compute (OMN-21190).

Occurrence keys are deterministic and namespaced, a moved anchor keeps its
lineage, and an anchor that cannot be re-found is classed ANCHOR_LOST and
counted.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

import omnimarket.nodes.node_review_finding_identity_compute as _node_pkg
from omnimarket.nodes.node_review_finding_identity_compute.handlers.handler_review_finding_identity import (
    HandlerReviewFindingIdentityCompute,
    claim_hash_for,
    hunk_hash_for,
)
from omnimarket.nodes.node_review_finding_identity_compute.models.enum_anchor_status import (
    EnumAnchorLostReason,
    EnumAnchorStatus,
)
from omnimarket.nodes.node_review_finding_identity_compute.models.model_review_finding_identity import (
    ModelFindingIdentityInput,
    ModelFindingIdentityOutput,
    ModelIdentityFinding,
    ModelPriorOccurrence,
    ModelReanchorRequest,
)

_CONTRACT_PATH = Path(_node_pkg.__file__).parent / "contract.yaml"

_HUNK = """\
def charge(order):
    total = order.total * 100
    return gateway.charge(total)
"""

_FILE_V1 = """\
import gateway


def charge(order):
    total = order.total * 100
    return gateway.charge(total)


def refund(order):
    return gateway.refund(order.total)
"""

# the same function, moved down and re-indented spacing; hunk lines unchanged
_FILE_V2_MOVED = """\
import gateway
import logging

log = logging.getLogger(__name__)


def refund(order):
    return gateway.refund(order.total)


def charge(order):
    total   = order.total * 100
    return gateway.charge(total)
"""

# a refactor that removes every line of the hunk
_FILE_V3_REFACTORED = """\
import gateway


class Billing:
    def __init__(self, client):
        self._client = client

    def collect(self, order):
        return self._client.submit(cents(order))
"""


def _finding(**overrides: object) -> ModelIdentityFinding:
    base: dict[str, object] = {
        "finding_ref": "f-1",
        "repository": "OmniNode-ai/omnimarket",
        "pr_number": 42,
        "snapshot_id": "snap-a",
        "path": "src/billing.py",
        "hunk_text": _HUNK,
        "category": "correctness",
        "rule_id": "float-money",
        "claim_text": "Money is computed with floats",
        "claim_ordinal": 0,
        "start_line": 4,
    }
    base.update(overrides)
    return ModelIdentityFinding.model_validate(base)


def _identity_of(finding: ModelIdentityFinding) -> tuple[str, str]:
    out = HandlerReviewFindingIdentityCompute().handle(
        ModelFindingIdentityInput(correlation_id=uuid4(), new_findings=(finding,))
    )
    (identity,) = out.identities
    assert identity.occurrence_key is not None
    return identity.occurrence_key, identity.lineage_id


def _prior(finding: ModelIdentityFinding) -> ModelPriorOccurrence:
    key, lineage = _identity_of(finding)
    return ModelPriorOccurrence(finding=finding, occurrence_key=key, lineage_id=lineage)


def _reanchor(
    prior: ModelPriorOccurrence,
    file_text: str | None,
    *,
    snapshot_id: str = "snap-b",
    target_path: str | None = None,
    finding_ref: str = "f-1",
) -> ModelReanchorRequest:
    return ModelReanchorRequest(
        finding_ref=finding_ref,
        prior=prior,
        target_snapshot_id=snapshot_id,
        target_path=target_path,
        target_file_text=file_text,
    )


def _run(
    *reanchors: ModelReanchorRequest, min_similarity: float = 0.85
) -> ModelFindingIdentityOutput:
    return HandlerReviewFindingIdentityCompute().handle(
        ModelFindingIdentityInput(
            correlation_id=uuid4(),
            reanchors=tuple(reanchors),
            min_similarity=min_similarity,
        )
    )


@pytest.mark.unit
class TestOccurrenceKey:
    """AC1: the same finding on the same anchor gets the same occurrence key."""

    def test_same_finding_same_anchor_same_key(self) -> None:
        a = _identity_of(_finding())
        b = _identity_of(_finding(finding_ref="f-other-emission"))
        assert a == b

    def test_key_is_stable_across_hunk_whitespace_and_line_numbers(self) -> None:
        noisy = "@@ -1,3 +9,3 @@\n\n  def charge(order):  \n        total =   order.total * 100\n    return gateway.charge(total)\n"
        assert _identity_of(_finding(hunk_text=noisy, start_line=9)) == _identity_of(
            _finding()
        )

    def test_lineage_of_a_new_finding_is_its_own_key(self) -> None:
        key, lineage = _identity_of(_finding())
        assert lineage == key

    @pytest.mark.parametrize(
        "override",
        [
            {"repository": "OmniNode-ai/omnibase_core"},
            {"pr_number": 43},
            {"snapshot_id": "snap-b"},
            {"path": "src/other.py"},
            {"hunk_text": "def other():\n    pass\n"},
            {"category": "security"},
            {"rule_id": "other-rule"},
            {"rule_id": None},
            {"claim_text": "A different claim about the same hunk"},
            {"claim_ordinal": 1},
        ],
    )
    def test_every_key_component_changes_the_key(
        self, override: dict[str, object]
    ) -> None:
        assert _identity_of(_finding(**override))[0] != _identity_of(_finding())[0]

    def test_two_violations_of_one_rule_in_one_hunk_do_not_collide(self) -> None:
        first = _identity_of(_finding(claim_ordinal=0))
        second = _identity_of(_finding(claim_ordinal=1))
        assert first[0] != second[0]

    def test_claim_text_is_normalized(self) -> None:
        assert claim_hash_for("Money  is\ncomputed WITH floats") == claim_hash_for(
            "money is computed with floats"
        )

    def test_hunk_hash_ignores_blank_lines_and_headers(self) -> None:
        assert hunk_hash_for("@@ -1 +1 @@\n\na = 1\n\n") == hunk_hash_for("a = 1")

    def test_counts_new_findings(self) -> None:
        out = HandlerReviewFindingIdentityCompute().handle(
            ModelFindingIdentityInput(
                correlation_id=uuid4(),
                new_findings=(_finding(), _finding(finding_ref="f-2", claim_ordinal=1)),
            )
        )
        assert out.counts.new == 2
        assert out.counts.total == 2
        assert {i.status for i in out.identities} == {EnumAnchorStatus.NEW}


@pytest.mark.unit
class TestLineage:
    """AC1: a moved anchor keeps its lineage."""

    def test_unmoved_anchor_in_new_head_is_unchanged_and_keeps_lineage(self) -> None:
        prior = _prior(_finding())
        out = _run(_reanchor(prior, _FILE_V1))
        (identity,) = out.identities
        assert identity.status is EnumAnchorStatus.UNCHANGED
        assert identity.lineage_id == prior.lineage_id
        assert identity.prior_occurrence_key == prior.occurrence_key
        assert identity.start_line == 4
        # a different head is a different occurrence, per the snapshot-id component
        assert identity.occurrence_key is not None
        assert identity.occurrence_key != prior.occurrence_key

    def test_moved_anchor_keeps_lineage_and_reports_the_new_line(self) -> None:
        prior = _prior(_finding())
        out = _run(_reanchor(prior, _FILE_V2_MOVED))
        (identity,) = out.identities
        assert identity.status is EnumAnchorStatus.MOVED
        assert identity.lineage_id == prior.lineage_id
        assert identity.start_line == 11
        assert identity.lost_reason is None
        assert out.counts.moved == 1
        assert out.counts.anchor_lost == 0

    def test_moved_anchor_key_equals_a_fresh_key_on_the_moved_hunk(self) -> None:
        prior = _prior(_finding())
        (identity,) = _run(_reanchor(prior, _FILE_V2_MOVED)).identities
        fresh_key, _ = _identity_of(_finding(snapshot_id="snap-b", start_line=11))
        assert identity.occurrence_key == fresh_key

    def test_reanchor_is_deterministic(self) -> None:
        prior = _prior(_finding())
        first = _run(_reanchor(prior, _FILE_V2_MOVED)).identities
        second = _run(_reanchor(prior, _FILE_V2_MOVED)).identities
        assert first == second

    def test_lineage_survives_a_second_move(self) -> None:
        prior = _prior(_finding())
        (moved,) = _run(_reanchor(prior, _FILE_V2_MOVED)).identities
        assert moved.occurrence_key is not None
        second_prior = ModelPriorOccurrence(
            finding=_finding(snapshot_id="snap-b", start_line=11),
            occurrence_key=moved.occurrence_key,
            lineage_id=moved.lineage_id,
        )
        (again,) = _run(
            _reanchor(second_prior, _FILE_V1, snapshot_id="snap-c")
        ).identities
        assert again.status is EnumAnchorStatus.MOVED
        assert again.lineage_id == prior.lineage_id

    def test_a_renamed_file_is_followed_through_target_path(self) -> None:
        prior = _prior(_finding())
        (identity,) = _run(
            _reanchor(prior, _FILE_V2_MOVED, target_path="src/billing_v2.py")
        ).identities
        assert identity.status is EnumAnchorStatus.MOVED
        assert identity.lineage_id == prior.lineage_id

    def test_a_lightly_edited_hunk_is_reanchored_by_similarity(self) -> None:
        edited = _FILE_V1.replace("order.total * 100", "order.total * 100  # cents")
        edited = edited.replace(
            "gateway.charge(total)", "gateway.charge(total, retry=3)"
        )
        prior = _prior(_finding())
        (identity,) = _run(_reanchor(prior, edited), min_similarity=0.5).identities
        assert identity.status is EnumAnchorStatus.UNCHANGED
        assert identity.lineage_id == prior.lineage_id
        assert identity.hunk_hash != hunk_hash_for(_HUNK)


@pytest.mark.unit
class TestAnchorLost:
    """AC2: an anchor that cannot be re-found is ANCHOR_LOST and counted."""

    def test_refactor_that_removes_the_hunk_is_anchor_lost(self) -> None:
        prior = _prior(_finding())
        out = _run(_reanchor(prior, _FILE_V3_REFACTORED))
        (identity,) = out.identities
        assert identity.status is EnumAnchorStatus.ANCHOR_LOST
        assert identity.lost_reason is EnumAnchorLostReason.HUNK_NOT_FOUND
        assert identity.occurrence_key is None
        assert identity.lineage_id == prior.lineage_id
        assert identity.prior_occurrence_key == prior.occurrence_key
        assert out.counts.anchor_lost == 1
        assert out.counts.total == 1

    def test_lost_anchors_are_counted_beside_found_ones(self) -> None:
        kept = _prior(_finding(finding_ref="keep"))
        lost = _prior(
            _finding(
                finding_ref="lost",
                hunk_text="def vanished():\n    return 1\n",
                claim_ordinal=3,
            )
        )
        out = _run(
            _reanchor(kept, _FILE_V2_MOVED, finding_ref="keep"),
            _reanchor(lost, _FILE_V2_MOVED, finding_ref="lost"),
        )
        by_ref = {i.finding_ref: i for i in out.identities}
        assert by_ref["keep"].status is EnumAnchorStatus.MOVED
        assert by_ref["lost"].status is EnumAnchorStatus.ANCHOR_LOST
        assert out.counts.moved == 1
        assert out.counts.anchor_lost == 1
        assert out.counts.total == 2

    def test_unavailable_file_is_anchor_lost_with_its_own_reason(self) -> None:
        prior = _prior(_finding())
        (identity,) = _run(_reanchor(prior, None)).identities
        assert identity.status is EnumAnchorStatus.ANCHOR_LOST
        assert identity.lost_reason is EnumAnchorLostReason.FILE_UNAVAILABLE

    def test_hunk_with_no_anchor_lines_is_anchor_lost(self) -> None:
        prior = _prior(_finding(hunk_text="@@ -1 +1 @@\n\n"))
        (identity,) = _run(_reanchor(prior, _FILE_V1)).identities
        assert identity.status is EnumAnchorStatus.ANCHOR_LOST
        assert identity.lost_reason is EnumAnchorLostReason.NO_ANCHOR_LINES

    def test_a_lost_anchor_never_gets_a_nearby_wrong_key(self) -> None:
        prior = _prior(_finding())
        (identity,) = _run(_reanchor(prior, _FILE_V3_REFACTORED)).identities
        assert identity.hunk_hash is None
        assert identity.start_line is None


@pytest.mark.unit
class TestInputValidation:
    def test_models_are_frozen_and_forbid_extras(self) -> None:
        finding = _finding()
        assert ModelIdentityFinding.model_config["frozen"] is True
        with pytest.raises(ValidationError):
            ModelIdentityFinding.model_validate({**finding.model_dump(), "extra": 1})

    def test_negative_ordinal_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            _finding(claim_ordinal=-1)


@pytest.mark.unit
class TestContract:
    def test_contract_declares_compute_node_and_topics(self) -> None:
        contract = yaml.safe_load(_CONTRACT_PATH.read_text())
        assert contract["name"] == "node_review_finding_identity_compute"
        assert contract["node_type"] == "compute"
        command = contract["runtime_dispatch"]["command_topic"]
        terminal = contract["runtime_dispatch"]["terminal_events"]["success"]
        assert command == "onex.cmd.omnimarket.review-finding-identity-resolve.v1"
        assert terminal == "onex.evt.omnimarket.review-finding-identity-resolved.v1"
        assert command in contract["event_bus"]["subscribe_topics"]
        assert terminal in contract["event_bus"]["publish_topics"]
        assert contract["terminal_event"] == terminal

    def test_contract_handler_resolves_to_the_handler_class(self) -> None:
        contract = yaml.safe_load(_CONTRACT_PATH.read_text())
        assert contract["handler"]["class"] == "HandlerReviewFindingIdentityCompute"
        assert (
            contract["handler"]["module"]
            == HandlerReviewFindingIdentityCompute.__module__
        )
