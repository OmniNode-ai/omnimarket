# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The L4 layer is asserted only where it is the enforcement surface (OMN-18963).

``ci.yml`` runs on ``push`` to main and hotfix branches as well as on
``pull_request`` and ``merge_group``. Layer 4 asserts check-run contexts minted
by OTHER workflow files, strictly and with no accepted absence. Enumerated live
on 2026-09-21, 32 of the 53 asserted contexts are owned by workflows declaring
no ``push`` trigger at all, so on a push run they cannot mint a check-run and
the poller waits for something that will never arrive until its deadline turns
the required verdict red.

``main`` in this repository is release-synced: the release workflow
fast-forwards it to an already-green dev commit, which fires ``ci.yml`` on
``push``. Every recent CI run on branch ``main`` failed this way, on a sha whose
own pull request was green -- and because that sha is simultaneously the ``dev``
head, the failure reads as a red dev head to anything looking up check state by
commit.

These tests pin the scoping AND its fail-closed edges. The property that
matters is the last one: merge admission is untouched.
"""

from __future__ import annotations

import pytest

from scripts.ci.ci_summary_gate import (
    EXPECTED_EXTERNAL_CONTEXTS,
    MERGE_ADMISSION_EVENTS,
    external_layer_applies,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("event", sorted(MERGE_ADMISSION_EVENTS))
def test_merge_admission_events_still_assert_the_layer(event: str) -> None:
    """The events on which a merge is actually admitted are unchanged."""

    assert external_layer_applies(event) is True


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
def test_non_admission_events_do_not_assert_the_layer(event: str) -> None:
    """A run whose event cannot mint these contexts does not wait for them."""

    assert external_layer_applies(event) is False


def test_absent_event_fails_closed() -> None:
    """A caller that forgets the event gets the strict reading, not a skip."""

    assert external_layer_applies(None) is True


def test_unknown_event_fails_closed() -> None:
    """An event nobody considered asserts the layer rather than skipping it.

    This is the direction that matters. A new trigger added to ``ci.yml``
    later must not silently drop L4 enforcement; it must show up as a wedge
    that someone has to reason about.
    """

    assert external_layer_applies("repository_dispatch") is True
    assert external_layer_applies("") is True


def test_pull_request_is_a_merge_admission_event() -> None:
    """Named outright: the gate that admits a merge keeps every context.

    If this ever goes false, the change has stopped being a scoping change
    and has become a weakening of merge admission.
    """

    assert "pull_request" in MERGE_ADMISSION_EVENTS
    assert external_layer_applies("pull_request") is True
    assert len(EXPECTED_EXTERNAL_CONTEXTS) > 0


def test_scoping_is_a_predicate_over_events_not_over_contexts() -> None:
    """No context is removed from the tuple by this change.

    The regression this guards against is a later edit "fixing" a wedge by
    deleting entries instead of scoping the event, which would drop them from
    the pull-request gate too.
    """

    for name in (
        "advisory-job-gate / advisory-job-gate",
        "Hostile Review Gate",
        "pr-title / check-title",
        "deploy-gate / deploy-gate",
    ):
        assert name in EXPECTED_EXTERNAL_CONTEXTS
