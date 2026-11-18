# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: the revoke cause rules, with the failing controls."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_claude_pool_revoke_cause_compute.handlers.handler_revoke_cause import (
    HandlerRevokeCause,
)
from omnimarket.nodes.node_claude_pool_revoke_cause_compute.models import (
    ModelRevokeCauseRequest,
)

pytestmark = pytest.mark.unit

#: Synthetic markers and texts: the deployed markers are the operator's, in the overlays repository.
MARKERS = ("synthetic-challenge-marker", "synthetic page title")
CHALLENGE_TEXT = "API Error: 403 <title>Synthetic Page Title</title> blocked"
AUTH_TEXT = "Failed to authenticate. API Error: 401 invalid authentication credentials"


def _decide(evidence: str | None, markers: tuple[str, ...] = MARKERS) -> str:
    return (
        HandlerRevokeCause()
        .handle(
            ModelRevokeCauseRequest(
                label="alpha", evidence=evidence, challenge_markers=markers
            )
        )
        .cause
    )


@pytest.mark.parametrize("evidence", [None, "", "   \n"])
def test_no_evidence_is_a_revoke(evidence: str | None) -> None:
    assert _decide(evidence) == "revoked"


def test_no_declared_markers_is_a_revoke() -> None:
    assert _decide(CHALLENGE_TEXT, markers=()) == "revoked"


def test_a_declared_marker_in_the_evidence_is_a_challenge() -> None:
    assert _decide(CHALLENGE_TEXT) == "challenged"


def test_the_marker_match_ignores_case() -> None:
    assert _decide("SYNTHETIC-CHALLENGE-MARKER") == "challenged"


def test_any_one_of_several_markers_is_enough() -> None:
    assert _decide("only the synthetic-challenge-marker is here") == "challenged"


def test_control_plain_auth_failure_is_not_a_challenge() -> None:
    # A handler that answers "challenged" for any evidence fails this.
    assert _decide(AUTH_TEXT) == "revoked"


def test_reason_never_echoes_evidence() -> None:
    for evidence in (CHALLENGE_TEXT, AUTH_TEXT):
        reason = (
            HandlerRevokeCause()
            .handle(
                ModelRevokeCauseRequest(
                    label="alpha", evidence=evidence, challenge_markers=MARKERS
                )
            )
            .reason
        )
        assert evidence not in reason
        assert all(marker not in reason.lower() for marker in MARKERS)
