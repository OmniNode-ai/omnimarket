# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 (OMN-19827): every occ_autobind_outcome marker line maps to exactly one outcome.

The live producer reports each consumed autobind command as a head-bound
check-run whose first summary line is the machine-readable marker
``occ-autobind-outcome: <KIND> repo=... pr=... correlation_id=... reason=...``.
The PR landing workflow reads a typed ``ModelPrLandingCompanionOutcome``
instead. These tests run the mapping over every marker line recorded on
2026-09-26 on a public repository, and over the live renderer's output for every
outcome the renderer can produce.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from omnimarket.events.pr_landing_companion import (
    EnumPrLandingCompanionDeclineCode,
    EnumPrLandingCompanionOp,
    EnumPrLandingCompanionOutcomeKind,
    ModelPrLandingCompanionOutcome,
)
from omnimarket.events.topics import PR_LANDING_COMPANION_OUTCOME_TOPIC_V1
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_autobind_outcome import (
    OUTCOME_MARKER_PREFIX,
    EnumAutobindOutcome,
    render_outcome_summary,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_autobind_outcome_reader import (
    companion_outcome_from_autobind_marker,
)

pytestmark = pytest.mark.unit

_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "tests"
    / "fixtures"
    / "pr_landing"
    / "companion_outcome"
    / "marker_lines_2026-09-26.json"
)
_HEAD = "b485552d9e9cb4c28079908f87dbbe1a1f23d602"

# The check-run conclusion occ_autobind_outcome posts for each kind.
_CONCLUSION_FOR_KIND = {
    EnumPrLandingCompanionOutcomeKind.MINTED: "SUCCESS",
    EnumPrLandingCompanionOutcomeKind.DECLINED: "NEUTRAL",
    EnumPrLandingCompanionOutcomeKind.ERROR: "FAILURE",
}


def _records() -> list[dict[str, object]]:
    doc = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    records: list[dict[str, object]] = doc["records"]
    return records


def test_recorded_corpus_is_the_day_and_non_trivial() -> None:
    records = _records()
    assert len(records) >= 100
    assert all(str(r["completed_at"]).startswith("2026-09-26") for r in records)
    assert all(str(r["marker_line"]).startswith(OUTCOME_MARKER_PREFIX) for r in records)
    kinds = {str(r["marker_line"]).split()[1] for r in records}
    assert {"DECLINED", "ERROR"} <= kinds


def test_every_recorded_marker_line_maps_to_exactly_one_outcome() -> None:
    for record in _records():
        line = str(record["marker_line"])
        outcome = companion_outcome_from_autobind_marker(
            line, head_sha=str(record["head_sha"])
        )
        assert isinstance(outcome, ModelPrLandingCompanionOutcome)
        assert outcome.kind.value == line.split()[1], line
        assert _CONCLUSION_FOR_KIND[outcome.kind] == record["conclusion"], line
        # The marker names the command's own PR. The fixture's pr_number is the
        # PR whose head carried the check-run; a head shared by stacked PRs
        # carries every one of their outcomes, so the two can differ.
        assert f"repo={outcome.repository} pr={outcome.pr_number} " in line, line
        assert outcome.repository == record["repository"], line
        assert outcome.head_sha == record["head_sha"], line
        assert outcome.op is EnumPrLandingCompanionOp.DERIVE
        if outcome.kind is EnumPrLandingCompanionOutcomeKind.DECLINED:
            assert (
                outcome.decline_code
                is not EnumPrLandingCompanionDeclineCode.UNCLASSIFIED
            ), line


def test_every_recorded_decline_names_its_companion_when_the_line_does() -> None:
    named = {
        EnumPrLandingCompanionDeclineCode.AUTHORED_UNVERIFIED,
        EnumPrLandingCompanionDeclineCode.ALREADY_BOUND,
        EnumPrLandingCompanionDeclineCode.STAMP_REBOUND,
    }
    seen: set[EnumPrLandingCompanionDeclineCode] = set()
    for record in _records():
        line = str(record["marker_line"])
        outcome = companion_outcome_from_autobind_marker(
            line, head_sha=str(record["head_sha"])
        )
        if outcome.decline_code in named:
            seen.add(outcome.decline_code)
            assert outcome.occ_pr is not None, line
            assert f"OCC#{outcome.occ_pr}" in line, line
    assert seen == named


@pytest.mark.parametrize(
    ("reason", "code", "occ_pr", "stamped"),
    [
        (
            "authored OCC companion Evidence-Source: OCC#11546 for OMN-19716 on "
            "OmniNode-ai/omnimarket#2974 (product head b485552d, branch "
            "auto/omninode-ai-omnimarket-pr-2974-occ-autobind) | OCC companion "
            "NOT verified: no OCC companion verifier wired; fail-closed",
            EnumPrLandingCompanionDeclineCode.AUTHORED_UNVERIFIED,
            11546,
            None,
        ),
        (
            "no-op: OmniNode-ai/omnimarket#2923 already bound to OCC#11336 "
            "(Evidence-Source already an OCC source)",
            EnumPrLandingCompanionDeclineCode.ALREADY_BOUND,
            11336,
            True,
        ),
        (
            "no-op: OmniNode-ai/omnimarket#2923 already carries exactly one stamp "
            "naming the proven companion OCC#11336 (OMN-18853)",
            EnumPrLandingCompanionDeclineCode.ALREADY_BOUND,
            11336,
            True,
        ),
        (
            "rebound evidence-source stamp on OmniNode-ai/omnimarket#2912 to the "
            "proven companion OCC#11417 (displaced OCC#11308; eligible at head "
            "ae968b50) (OMN-18853)",
            EnumPrLandingCompanionDeclineCode.STAMP_REBOUND,
            11417,
            True,
        ),
        (
            "skip:LEASE_HELD — OmniNode-ai/omniintelligence#946@71d1c238 companion "
            "already being minted by another producer",
            EnumPrLandingCompanionDeclineCode.LEASE_HELD,
            None,
            None,
        ),
        (
            "skip:OCC_SELF_COMPANION — OmniNode-ai/onex_change_control#1 is itself "
            "an OCC evidence record",
            EnumPrLandingCompanionDeclineCode.OCC_SELF_COMPANION,
            None,
            None,
        ),
        (
            "skip:WINDOW_IN_FLIGHT — OmniNode-ai/omnimarket#2974 was not pushed to "
            "the OmniNode-ai/omnimarket batch window companion: OCC#11963 head "
            "0123abcd has 2 check run(s) still running (OMN-20042)",
            EnumPrLandingCompanionDeclineCode.WINDOW_IN_FLIGHT,
            None,
            None,
        ),
        (
            "[dry-run] would author OCC companion for OMN-1 on OmniNode-ai/x#1 "
            "(no side effects performed)",
            EnumPrLandingCompanionDeclineCode.DRY_RUN,
            None,
            None,
        ),
        (
            "skip:SOMETHING_NEW — a code this mapping has never seen",
            EnumPrLandingCompanionDeclineCode.UNCLASSIFIED,
            None,
            None,
        ),
    ],
)
def test_decline_reasons_classify(
    reason: str,
    code: EnumPrLandingCompanionDeclineCode,
    occ_pr: int | None,
    stamped: bool | None,
) -> None:
    line = render_outcome_summary(
        outcome=EnumAutobindOutcome.DECLINED,
        reason=reason,
        repo="OmniNode-ai/omnimarket",
        pr_number=2974,
        correlation_id="057aad1c-edf0-4a25-a748-ed8ced27cd76",
    ).splitlines()[0]
    outcome = companion_outcome_from_autobind_marker(line, head_sha=_HEAD)
    assert outcome.kind is EnumPrLandingCompanionOutcomeKind.DECLINED
    assert outcome.decline_code is code
    assert outcome.occ_pr == occ_pr
    assert outcome.stamped is stamped
    assert outcome.decline_reason == " ".join(reason.split())
    assert outcome.correlation_id == UUID("057aad1c-edf0-4a25-a748-ed8ced27cd76")


@pytest.mark.parametrize("legacy", list(EnumAutobindOutcome))
def test_every_outcome_the_live_renderer_can_write_maps_back(
    legacy: EnumAutobindOutcome,
) -> None:
    reason = {
        EnumAutobindOutcome.MINTED: (
            "authored OCC companion Evidence-Source: OCC#11546 for OMN-19716 on "
            "OmniNode-ai/omnimarket#2974 (product head b485552d, branch "
            "auto/omninode-ai-omnimarket-pr-2974-occ-autobind)"
        ),
        EnumAutobindOutcome.DECLINED: "skip:PR_DRAFT — not a mergeable product PR",
        EnumAutobindOutcome.ERROR: "failed: git push rejected",
    }[legacy]
    line = render_outcome_summary(
        outcome=legacy,
        reason=reason,
        repo="OmniNode-ai/omnimarket",
        pr_number=2974,
        correlation_id=None,
    ).splitlines()[0]
    outcome = companion_outcome_from_autobind_marker(line, head_sha=_HEAD)
    assert outcome.kind.value == legacy.value
    assert outcome.correlation_id is None
    if legacy is EnumAutobindOutcome.MINTED:
        assert outcome.occ_pr == 11546
        assert outcome.stamped is True
    if legacy is EnumAutobindOutcome.ERROR:
        assert outcome.error_reason == reason


@pytest.mark.parametrize(
    "line",
    [
        "",
        "not a marker line",
        "occ-autobind-outcome: MAYBE repo=OmniNode-ai/x pr=1 correlation_id=unknown reason=x",
        "occ-autobind-outcome: DECLINED repo=OmniNode-ai/x correlation_id=unknown reason=x",
    ],
)
def test_a_line_that_is_not_a_marker_is_refused(line: str) -> None:
    with pytest.raises(ValueError, match="marker"):
        companion_outcome_from_autobind_marker(line, head_sha=_HEAD)


def test_the_outcome_model_refuses_contradictory_fields() -> None:
    base: dict[str, object] = {
        "repository": "OmniNode-ai/omnimarket",
        "pr_number": 2974,
        "head_sha": _HEAD,
    }
    with pytest.raises(ValidationError):
        ModelPrLandingCompanionOutcome.model_validate({**base, "kind": "MINTED"})
    with pytest.raises(ValidationError):
        ModelPrLandingCompanionOutcome.model_validate({**base, "kind": "DECLINED"})
    with pytest.raises(ValidationError):
        ModelPrLandingCompanionOutcome.model_validate({**base, "kind": "ERROR"})
    with pytest.raises(ValidationError):
        ModelPrLandingCompanionOutcome.model_validate(
            {**base, "kind": "ERROR", "error_reason": "x", "decline_reason": "y"}
        )
    minted = ModelPrLandingCompanionOutcome.model_validate(
        {**base, "kind": "MINTED", "occ_pr": 11546, "stamped": True}
    )
    assert minted.armed is None
    assert minted.conflicting is None


def test_the_outcome_topic_is_the_frozen_seam_name() -> None:
    assert (
        PR_LANDING_COMPANION_OUTCOME_TOPIC_V1
        == "onex.evt.omnimarket.pr-landing-companion-outcome.v1"
    )
