# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane-work leases keep silent claim owners live (OMN-21428)."""

import pytest

from omnimarket.handlers import rules_merge_sweep as rules

pytestmark = pytest.mark.unit

LEASE = "6f1c2a8e-1b7d-4c55-9d1e-2f3a4b5c6d7e"
CLAIM = (
    "2026-10-26T03:00:00Z | CLAIM | lane=lw-a | principal=p1 | ticket=OMN-21428 | "
    "actor=script:node_projection_lease | model=none | host=h200 | run=r1 | parent=orch-1 | "
    f"kind=lane-work | resource=lane:lw-a | lease={LEASE} | fence=7"
)
TERMINAL = (
    "2026-10-26T05:00:00Z | TERMINAL | lane=lw-a | principal=p1 | ticket=OMN-21428 | "
    "actor=script:node_projection_lease | model=none | friction=none | outcome=unknown | "
    f"cause=lease-expired | closes-CLAIM=2026-10-26T03:00:00Z | lease={LEASE} | fence=7 | "
    "delegated=0 delegation_reason=no-text-or-code"
)


@pytest.mark.parametrize("text", [CLAIM, CLAIM.split(" | ", 2)[2]])
def test_lane_work_lease_reads_full_and_parsed_claim_text(text: str) -> None:
    assert rules.lane_work_lease(text) == LEASE


@pytest.mark.parametrize(
    "text",
    [
        CLAIM.replace("actor=script:node_projection_lease", "actor=claims-lane"),
        CLAIM.replace(" | kind=lane-work", ""),
        CLAIM.replace(f" | lease={LEASE}", ""),
        CLAIM.replace(LEASE, "not-a-uuid"),
        CLAIM.replace(" | CLAIM | ", " | STATUS | "),
        CLAIM.replace(
            "actor=script:node_projection_lease",
            "note=actor=script:node_projection_lease",
        ),
        CLAIM.replace(" | kind=lane-work", "") + "\nkind=lane-work",
        CLAIM.replace("actor=script:node_projection_lease", "actor=claims-lane")
        + " | actor=script:node_projection_lease",
        CLAIM.replace("kind=lane-work", "kind=other") + " | kind=lane-work",
        CLAIM.replace(LEASE, "invalid") + f" | lease={LEASE}",
    ],
)
def test_lane_work_lease_requires_its_own_first_cells(text: str) -> None:
    assert rules.lane_work_lease(text) is None


def test_lane_work_lease_ignores_later_duplicate_cells() -> None:
    assert (
        rules.lane_work_lease(
            CLAIM + " | actor=claims-lane | kind=other | lease=invalid"
        )
        == LEASE
    )


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        ([CLAIM], {"lw-a"}),
        ([CLAIM, TERMINAL], set()),
        ([TERMINAL, CLAIM], {"lw-a"}),
        ([CLAIM, TERMINAL.replace("lane=lw-a", "lane=other")], {"lw-a"}),
        (
            [CLAIM, "2026-10-26T03:00:00Z | CLAIM | lane=lw-a | pr=omnimarket#42"],
            {"lw-a"},
        ),
    ],
)
def test_leased_lanes_follows_ledger_order(
    lines: list[str], expected: set[str]
) -> None:
    assert rules.leased_lanes(rules.parse_rows(lines)) == expected


@pytest.mark.parametrize(
    ("lease_claim", "state"),
    [
        (CLAIM, "live"),
        (None, "stale"),
        (
            CLAIM.replace("actor=script:node_projection_lease", "actor=claims-lane"),
            "stale",
        ),
    ],
)
def test_silent_pr_owner_with_lane_work_lease(
    lease_claim: str | None, state: str
) -> None:
    lines = [lease_claim] if lease_claim else []
    lines.append("2026-10-26T03:00:00Z | CLAIM | lane=lw-a | pr=omnimarket#42")
    owner = rules.claim_owner(
        "omnimarket#42", None, rules.parse_rows(lines), "2026-10-26T05:00:00Z"
    )
    assert owner["state"] == state
    assert owner["lane"] == "lw-a"
