# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain of the skip-cause naming of the lab-fill lane-states operation (OMN-23025).

The live send job labels every brief-build exit other than the check's refusal (3) as
brief-rules-unreadable, whichever step failed. The ledger shows three different causes under
that one label on 2026-11-08: the 07:50Z fire's 29 and the 08:10Z fire's 22 skipped lanes ended
on Linear's HTTP 429 in the ticket reads (lab-fill.js, send script comment), and the 09:10Z to
09:50Z fires' 8 ended on the absent ticket_mirror overlay (lane mirror-overlay-hotfix-1000). The
details below are the shapes those steps print; the rules reader's own shapes come from
lane_rules_block.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_lab_fill_plan_compute.handlers import (
    HandlerLabFillLaneStates,
)
from omnimarket.nodes.node_lab_fill_plan_compute.handlers.handler_lab_fill_lane_states import (
    name_skip_cause,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models import (
    ModelLabFillLaneStatesRequest,
)

RATE_LIMIT = "urllib.error.HTTPError: HTTP Error 429: Too Many Requests"
MIRROR = (
    "the ticket_mirror overlay is not installed (TICKET_MIRROR_OVERLAY_PATH unset; "
    "no overlay root holds ticket_mirror/overlay.yaml)"
)
RULES_MISSING = "lane rules block is missing: /srv/rh/lane_rules/lane-rules-block.md"
RULES_UNREADABLE = "cannot read lane rules block /srv/rh/lane_rules/lane-rules-block.md: [Errno 13] Permission denied"
NO_CHECKOUT = (
    "onex-ledger resolver: no valid rules clone. PROJECT_ENV tried: <unset>; "
    "$OMNI_HOME/../rules_home tried: /srv/rules_home."
)
MARKER = "brief standing rules lack Canonical shape: new capability is a node"


def settle(detail: str, outcome: str = "skipped:brief-rules-unreadable") -> Any:
    return HandlerLabFillLaneStates().handle(
        ModelLabFillLaneStatesRequest(
            placements=(
                {
                    "lane": "lab-fill-omn22053-11080810",
                    "host": "h051",
                    "outcome": outcome,
                    "detail": detail,
                },
            )
        )
    )


def test_golden_the_08_10z_rate_limit_skip_is_named_for_the_rate_limit_not_the_rules() -> (
    None
):
    result = settle(RATE_LIMIT)
    assert result.refusals_cell == (
        "refusals=lab-fill-omn22053-11080810@h051:brief-ticket-rate-limited"
    )
    assert result.never_started == ("lab-fill-omn22053-11080810",)


def test_golden_the_09_30z_mirror_skip_is_named_for_the_overlay() -> None:
    assert settle(MIRROR).refusals_cell == (
        "refusals=lab-fill-omn22053-11080810@h051:brief-mirror-overlay-missing"
    )


@pytest.mark.parametrize("detail", [RULES_MISSING, RULES_UNREADABLE])
def test_a_rules_file_failure_names_the_file(detail: str) -> None:
    assert settle(detail).refusals_cell == (
        "refusals=lab-fill-omn22053-11080810@h051:"
        "brief-rules-unreadable:/srv/rh/lane_rules/lane-rules-block.md"
    )


def test_a_missing_rules_checkout_names_the_resolver() -> None:
    assert name_skip_cause("brief-rules-unreadable", NO_CHECKOUT) == (
        "brief-rules-unreadable:rules-checkout"
    )


def test_a_marker_missing_from_the_rules_block_is_a_rules_failure() -> None:
    assert name_skip_cause("brief-rules-unreadable", MARKER) == (
        "brief-rules-unreadable:standing-rules-marker"
    )


def test_error_chain_a_detail_no_rule_knows_keeps_the_label_the_dispatch_gave() -> None:
    assert name_skip_cause("brief-rules-unreadable", "something else broke") == (
        "brief-rules-unreadable"
    )
    assert name_skip_cause("brief-rules-unreadable", "") == "brief-rules-unreadable"


def test_error_chain_only_the_catch_all_label_is_renamed() -> None:
    """A lane the check refused, or cut at its slice, keeps its own reason whatever its detail says."""
    assert name_skip_cause("brief-check-refused", RATE_LIMIT) == "brief-check-refused"
    assert name_skip_cause("lane-timeout:40s", MIRROR) == "lane-timeout:40s"


def test_error_chain_a_number_that_only_contains_429_is_not_a_rate_limit() -> None:
    assert name_skip_cause("brief-rules-unreadable", "lane omn14290 build failed") == (
        "brief-rules-unreadable"
    )


def test_error_chain_a_rate_limit_beside_a_rules_failure_names_the_rules_file() -> None:
    """The rules reader runs first: its failure is the one that stopped the brief."""
    both = f"{RATE_LIMIT} / {RULES_MISSING}"
    assert name_skip_cause("brief-rules-unreadable", both).startswith(
        "brief-rules-unreadable:/srv/rh/lane_rules"
    )
