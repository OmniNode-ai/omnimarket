# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15300: the OCC observation PR shape must satisfy the merge-eligibility gate.

Driven against the REAL gate — ``validate_occ_merge_eligibility`` and its
``_extract_ticket_ids``, imported from ``omnibase_core``, are the same callables
the ``occ-preflight / eligibility`` and ``verify / verify`` checks execute. No
re-derived regex, no surrogate.

The defect these lock down: every PR this producer opened carried a title with
no ``OMN-`` token and a body whose only ticket mention was prose, so the gate
returned ``missing_ticket`` and the PR could never merge. Over 101 such PRs were
opened and none merged; the observation store's write path was dead for ~24h.
The 34 that DID merge were hand-renamed after creation — the producer itself
only ever emitted the failing shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_core.validation.validator_occ_merge_eligibility import (
    ModelOccEligibilityInput,
    validate_occ_merge_eligibility,
)
from omnibase_core.validation.validator_receipt_gate import _extract_ticket_ids

from omnimarket.nodes.node_occ_observation_effect.handlers.handler_occ_observation_effect import (
    render_occ_observation_commit_subject,
    render_occ_observation_pr_body,
    render_occ_observation_pr_title,
)
from omnimarket.nodes.node_occ_observation_effect.models.model_occ_observation_effect_request import (
    ModelOccObservationEffectRequest,
)

TICKET = "OMN-14888"
RELPATH = (
    "drift/occ_observations/OmniNode-ai__omnimarket/pr-1925/"
    "44cc2615aeaa0f98ecf4193aa3ca3a546ee8e08b__v1__run30327410003-1.yaml"
)
BRANCH = (
    "auto/occ-observation-drift-occ-observations-omninode-ai--omnimarket-pr-1925-"
    "44cc2615aeaa0f98ecf4193aa3ca3a546ee8e08b--v1--run30327410003-1-yaml"
)

# The exact strings OCC#5239 carried, copied from the failing run's env dump
# (job 90175734727). This is the shape the producer emitted before this fix.
LEGACY_TITLE = (
    "evidence: OCC observation append "
    "(44cc2615aeaa0f98ecf4193aa3ca3a546ee8e08b__v1__run30327410003-1.yaml)"
)
LEGACY_BODY = (
    "Deterministic, append-only OCC observation record authored by "
    "node_occ_observation_effect (OMN-14888). Adds exactly one net-new file: "
    f"`{RELPATH}`."
)


def _snapshot(
    title: str, body: str, commit_texts: tuple[str, ...], contracts_dir: Path
) -> ModelOccEligibilityInput:
    return ModelOccEligibilityInput(
        repo="OmniNode-ai/onex_change_control",
        pr_number=5239,
        pr_title=title,
        pr_body=body,
        pr_branch=BRANCH,
        pr_commit_shas=("ed215caa3efa153ae2d0b7c84c4d57b41ea3299b",),
        pr_commit_texts=commit_texts,
        occ_commit_sha="ed215caa3efa153ae2d0b7c84c4d57b41ea3299b",
        contracts_dir=str(contracts_dir),
        receipts_dir=str(contracts_dir),
    )


class TestLegacyShapeIsRejected:
    """RED: the shape the producer used to emit fails the real gate."""

    def test_legacy_body_and_title_extract_no_ticket(self) -> None:
        assert _extract_ticket_ids(LEGACY_BODY, LEGACY_TITLE) == []

    def test_legacy_shape_fails_eligibility_as_missing_ticket(
        self, tmp_path: Path
    ) -> None:
        result = validate_occ_merge_eligibility(
            _snapshot(
                LEGACY_TITLE,
                LEGACY_BODY,
                (f"evidence: OCC observation append {RELPATH}",),
                tmp_path,
            )
        )
        assert result.eligible is False
        assert result.reason.value == "missing_ticket"
        assert result.ticket_ids == ()


class TestBindableShapeIsAccepted:
    """GREEN: the shape the producer emits now clears extraction AND binding."""

    def test_extracts_exactly_the_intended_ticket(self) -> None:
        title = render_occ_observation_pr_title(TICKET, RELPATH)
        body = render_occ_observation_pr_body(TICKET, RELPATH)
        assert _extract_ticket_ids(body, title) == [TICKET]

    def test_clears_missing_ticket_and_ticket_mismatch(self, tmp_path: Path) -> None:
        """The two failure modes that kept every observation PR unmergeable.

        A ticket must be both CITED (``_extract_ticket_ids``) and BOUND
        (``_ticket_bound_to_pr``); citing without binding trades
        ``missing_ticket`` for ``pr_ticket_mismatch`` and merges exactly as
        often. What remains after this point is contract/receipt resolution
        against a real OCC checkout, which this hermetic tmp_path cannot
        provide and which is not what OMN-15300 broke.
        """
        result = validate_occ_merge_eligibility(
            _snapshot(
                render_occ_observation_pr_title(TICKET, RELPATH),
                render_occ_observation_pr_body(TICKET, RELPATH),
                (render_occ_observation_commit_subject(TICKET, RELPATH),),
                tmp_path,
            )
        )
        assert result.reason.value not in {"missing_ticket", "pr_ticket_mismatch"}
        assert result.ticket_ids == (TICKET,)

    def test_binding_survives_on_each_axis_independently(self, tmp_path: Path) -> None:
        """Title, commit subject and Evidence-Ticket line each bind on their own.

        Any single one being hand-edited away later must not re-break the gate.
        """
        body = render_occ_observation_pr_body(TICKET, RELPATH)
        title = render_occ_observation_pr_title(TICKET, RELPATH)
        commit = render_occ_observation_commit_subject(TICKET, RELPATH)
        body_without_evidence_line = "\n".join(
            line
            for line in body.splitlines()
            if not line.startswith("Evidence-Ticket:")
        )

        axes = {
            "evidence-ticket line only": _snapshot(LEGACY_TITLE, body, (), tmp_path),
            "title only": _snapshot(title, body_without_evidence_line, (), tmp_path),
            "commit subject only": _snapshot(
                LEGACY_TITLE, body_without_evidence_line, (commit,), tmp_path
            ),
        }
        for axis, snapshot in axes.items():
            result = validate_occ_merge_eligibility(snapshot)
            assert result.reason.value != "pr_ticket_mismatch", axis
            assert result.ticket_ids == (TICKET,), axis


class TestDoesNotRegressTitleScanOverDemand:
    """The emitted shape must not widen gate scope (OMN-15194 / OMN-14658).

    Those tickets describe the title-scan fallback pulling every ``OMN-`` token
    in a title into gate scope. Putting a token in the title touches that path
    directly, so the shape is checked under both regimes.
    """

    def test_title_carries_exactly_one_ticket_token(self) -> None:
        import re

        tokens = re.findall(
            r"OMN-\d+", render_occ_observation_pr_title(TICKET, RELPATH)
        )
        assert tokens == [TICKET]

    def test_body_match_suppresses_the_title_fallback_entirely(self) -> None:
        """A hostile title cannot widen scope while the body carries the keyword.

        ``_extract_ticket_ids`` returns body closing-keyword matches
        EXCLUSIVELY, so the title is never scanned — this producer sits outside
        the over-demand path structurally, not by convention.
        """
        hostile_title = "evidence(OMN-14888): touches OMN-9999 and OMN-1234"
        body = render_occ_observation_pr_body(TICKET, RELPATH)
        assert _extract_ticket_ids(body, hostile_title) == [TICKET]

    def test_body_does_not_use_an_auto_close_keyword(self) -> None:
        """``Implements`` binds the gate without closing the ticket on merge.

        The extractor accepts Closes/Fixes/Resolves/Implements; only the first
        three are GitHub/Linear auto-close keywords. These PRs merge many times
        a day and must not close the ticket they report on.
        """
        body = render_occ_observation_pr_body(TICKET, RELPATH)
        assert f"Implements {TICKET}" in body
        for keyword in ("Closes", "Fixes", "Resolves"):
            assert f"{keyword} {TICKET}" not in body


class TestEvidenceTicketIsConstrained:
    def test_request_rejects_a_non_ticket_value(self) -> None:
        """A malformed ticket must fail at the request boundary.

        Reaching the PR-open call with an unbindable value would recreate the
        outage one PR at a time.
        """
        with pytest.raises(ValueError, match="evidence_ticket"):
            ModelOccObservationEffectRequest.model_validate(
                {"record": None, "evidence_ticket": "not-a-ticket"}
            )
