# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The class rules read the 19 FRICTION rows of 2026-11-09T11:06Z to 12:31Z that carry a bare ``class=other`` (OMN-23319).

The floor's ``stoppage:other`` row grew by these 19 rows (about 493 lane-minutes) after the rollup learned to read a bare
``class=other`` through its class rules. Each row below is the text of a real ledger row (lane, cost cell, text). Through the shipped
rules every one names its real class, none is dropped and no cost moves. The controls: the rules as they stood leave all 19 as
``other``; removing one new class, or reverting one widened pattern, sends only that cause's rows back to ``other``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.models.friction_rollup import (
    ModelFrictionClassRule,
    ModelFrictionRollupRequest,
    ModelFrictionRollupSource,
)
from omnimarket.nodes.node_friction_rollup_compute.handlers.handler_friction_rollup import (
    HandlerFrictionRollup,
)

pytestmark = pytest.mark.unit

OVERLAY = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_friction_rollup_compute/overlay.example.yaml"
)
NOW = datetime(2026, 11, 9, 14, 0, 0, tzinfo=UTC)

NEW_CLASSES = (
    "change-waits-on-an-upstream-pr-that-is-red-or-unmerged",
    "blocker-needs-an-operator-owned-input",
    "fill-ticket-start-when-line-unmet",
)
# The patterns of the classes that already owned a cause, as they stood before this change.
WIDENED_BEFORE = {
    "criterion-needs-a-host-or-runtime-the-lane-cannot-reach": r"(?:AC\d+ needs an? (?:container roll|live read)|this lane is on a different host)",
    "fill-ticket-already-merged-or-superseded-criteria-not-reconciled": r"(?:falsifiers target a stale|is superseded by OMN-\d+)",
    "lab-host-lacks-node-toolchain": r"(?:no node, pnpm or corepack|host with no node|h\d+ has no node|no pool host has node|node not installed on any pool host|Chromium can.t load its shared libraries)",
    "lab-run-placement-refused-no-host-admits-the-run": r"(?:placement refused|exit 75|no lab host admitted|NO-UNIT-SLOT)",
}

UPSTREAM = "change-waits-on-an-upstream-pr-that-is-red-or-unmerged"
OPERATOR = "blocker-needs-an-operator-owned-input"
START_WHEN = "fill-ticket-start-when-line-unmet"
UNREACHABLE = "criterion-needs-a-host-or-runtime-the-lane-cannot-reach"
MERGED = "fill-ticket-already-merged-or-superseded-criteria-not-reconciled"
NODE = "lab-host-lacks-node-toolchain"
PLACEMENT = "lab-run-placement-refused-no-host-admits-the-run"

# (lane, cost cell, text, the class the shipped rules must name, lane-minutes)
ROWS: tuple[tuple[str, str, str, str, float], ...] = (
    (
        "j8carry-0950",
        "~68 lane-minutes",
        "BLOCKER: #5401 cannot go green until #5574 and #5572 merge; both are red and owned by other lanes.",
        UPSTREAM,
        68.0,
    ),
    (
        "fill-ticket-OMN-22787-20261109T1038Z",
        "~7 lane-minutes",
        "BLOCKER: none (AC4 needs an h053 overlay change and install, which are out of scope for this lane.)",
        UNREACHABLE,
        7.0,
    ),
    (
        "fill-ticket-OMN-22560-20261109T1110Z",
        "~3 lane-minutes",
        "BLOCKER: the remaining AC4 work is entirely in `lane_brief.py`, which 3a.7 puts out of scope (`H_LAB` / `LAB_MARKERS`).",
        UNREACHABLE,
        3.0,
    ),
    (
        "edgeident-0950",
        "~85 lane-minutes",
        "BLOCKER: AC2 and the onex-api drop need #5576 and #5579 merged and a dev deploy first.",
        UPSTREAM,
        85.0,
    ),
    (
        "lag2-1020",
        "~54 lane-minutes",
        "BLOCKER: omnibase_infra#5810 is red on `lab-proof-receipt` at dfac84904261 and has no accepted landing handoff, so dev cannot carry it for #5840.",
        UPSTREAM,
        54.0,
    ),
    (
        "fill-ticket-OMN-22815-20261109T1110Z",
        "~2 lane-minutes",
        "BLOCKER: AC2 needs a lab broker read. The dev broker closes the connection from h101 (`kcat -L` returns no metadata), "
        "and a pre-PR slot can't be built from this lane.",
        UNREACHABLE,
        2.0,
    ),
    (
        "fill-ticket-OMN-22810-20261109T1110Z",
        "~7 lane-minutes",
        "BLOCKER: browser proof for AC2 and AC3 could not run: Chromium on h051 is missing `libatk-1.0.so.0`, and the lab-run "
        "placement found no free pool host (h201: NO-UNIT-SLOT).",
        NODE,
        7.0,
    ),
    (
        "fill-ticket-OMN-22912-20261109T1110Z",
        "~5 lane-minutes",
        "BLOCKER: AC3 plants a dev-only key in the real store, which is a credential lifecycle change. Rule 22 requires an "
        "OPERATOR-CONSENT row naming the credential. I searched the ledger and its archive and found none.",
        OPERATOR,
        5.0,
    ),
    (
        "gwjoblog-0955",
        "~78 lane-minutes",
        "BLOCKER: #5568 Split 4/20 and Split 1/20, and #5580 Split 2/20, are red with causes unread; the verb is not deployed, "
        "so cached job logs can't be filled yet",
        UPSTREAM,
        78.0,
    ),
    (
        "fill-ticket-OMN-22944-20261109T1143Z",
        "~9 lane-minutes",
        "BLOCKER: the Start-when line (piece 7 reads WORKING) is false; the piece 7 process-spine reading is UNKNOWN as of 2026-11-09T11:15:26Z.",
        START_WHEN,
        9.0,
    ),
    (
        "fill-ticket-OMN-22961-20261109T1143Z",
        "~9 lane-minutes",
        "BLOCKER: omnimarket#5074 (the dispatch entry node) is an OPEN, paused draft with red CI, so the J11 join test has nothing on dev to join",
        UPSTREAM,
        9.0,
    ),
    (
        "fill-ticket-OMN-22999-20261109T1143Z",
        "~6 lane-minutes",
        "BLOCKER: the change belongs in the private tables and needs the collaborator's user and paths from the operator.",
        OPERATOR,
        6.0,
    ),
    (
        "fill-ticket-OMN-22986-20261109T1143Z",
        "~9 lane-minutes",
        "BLOCKER: `node` is absent on h053, and no recorded `lab-fill.js` ticks exist, so AC1 parity can't be built or checked here.",
        NODE,
        9.0,
    ),
    (
        "fill-ticket-OMN-23000-20261109T1143Z",
        "~5 lane-minutes",
        "BLOCKER: the remaining work needs the real host inventory and marks-source details for the overlay, which I can't verify from this host.",
        UNREACHABLE,
        5.0,
    ),
    (
        "fill-ticket-OMN-22988-20261109T1143Z",
        "~8 lane-minutes",
        "BLOCKER: AC2's deletion half has no target and its falsifier paths hold no tests; the parity work is already merged in omnimarket#5346.",
        MERGED,
        8.0,
    ),
    (
        "fill-repair-running-vs-merged-launchd-lib-h200",
        "~3 lane-minutes",
        "BLOCKER: the repair must run on h200 (operator Mac), which this h105 lane can't reach; the installer would change h105 instead.",
        UNREACHABLE,
        3.0,
    ),
    (
        "fill-repair-running-vs-merged-plugin-h200",
        "~4 lane-minutes",
        "BLOCKER: the h200 plugin tree is a lab branch install held by lane proofs and the landing-controller hold, and the "
        "sanctioned `update-plugin` is fenced.",
        UNREACHABLE,
        4.0,
    ),
    (
        "deadlock-1130",
        "~77 lane-minutes",
        "BLOCKER: no valid proof host this run: one lab host has no Docker, another is away, and the one slot run was killed by my own timeout.",
        PLACEMENT,
        77.0,
    ),
    (
        "dlg-r7-1150",
        "~54 lane-minutes",
        "BLOCKER: R7 pass needs the unset variable, the missing grants and a resolving stability lane (operator-owned).",
        OPERATOR,
        54.0,
    ),
)
TOTAL_MINUTES = 493.0


def shipped_rules() -> list[ModelFrictionClassRule]:
    document = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))
    return [ModelFrictionClassRule(**item) for item in document["class_rules"]]


def rules_before() -> list[ModelFrictionClassRule]:
    """The shipped rules as they stood: no new class, the widened patterns as they were."""
    return [
        rule.model_copy(update={"pattern": WIDENED_BEFORE.get(rule.name, rule.pattern)})
        for rule in shipped_rules()
        if rule.name not in NEW_CLASSES
    ]


def ledger_text() -> str:
    lines = []
    for index, (lane, cost, text, _, _) in enumerate(ROWS):
        stamp = (NOW - timedelta(hours=1, minutes=index)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(
            f"{stamp} | FRICTION | lane={lane} | ticket=OMN-23319 | cost={cost} | class=other | req=x | via=bus:h200 | {text}"
        )
    return "\n".join(lines) + "\n"


def roll_report(rules: list[ModelFrictionClassRule]) -> dict[str, object]:
    request = ModelFrictionRollupRequest(
        sources=[
            ModelFrictionRollupSource(
                display="friction_rows.md", kind="markdown", text=ledger_text()
            )
        ],
        since=(NOW - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        until=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        generated_at=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        bucket_hours=2,
        umbrella_tickets=["OMN-17427"],
        aliases=[],
        class_rules=rules,
    )
    return HandlerFrictionRollup().handle(request).report


def roll(rules: list[ModelFrictionClassRule]) -> dict[str, dict[str, float]]:
    classes = roll_report(rules)["classes"]
    assert isinstance(classes, list)
    return {
        item["class"]: {
            "occurrences": item["occurrences"],
            "cost_minutes": item["cost_minutes"],
        }
        for item in classes
    }


def expected_by_class() -> dict[str, dict[str, float]]:
    expected: dict[str, dict[str, float]] = {}
    for _, _, _, name, minutes in ROWS:
        entry = expected.setdefault(name, {"occurrences": 0, "cost_minutes": 0.0})
        entry["occurrences"] += 1
        entry["cost_minutes"] += minutes
    return expected


def test_friction_other_class_shipped_rules_name_each_of_the_nineteen_rows() -> None:
    classes = roll(shipped_rules())
    assert "other" not in classes
    assert classes == expected_by_class()


def test_friction_other_class_no_row_is_dropped_and_no_cost_moves() -> None:
    """Never drop a row from the count: the 19 rows and their lane-minutes are the same before and after."""
    for rules in (shipped_rules(), rules_before()):
        totals = roll_report(rules)["totals"]
        assert isinstance(totals, dict)
        assert totals["friction_rows"] == len(ROWS) == 19
        assert totals["cost_minutes"] == TOTAL_MINUTES


# One row names two causes: Chromium lacks a library on h051 and the placement found no free pool host. The toolchain rule goes first;
# without it the placement rule names the row.
SECOND_CAUSE = {"fill-ticket-OMN-22810-20261109T1110Z": PLACEMENT}


def landed(names: set[str]) -> dict[str, int]:
    """Where each row lands when the classes in ``names`` are gone or back to their old patterns."""
    result: dict[str, int] = {}
    for lane, _, text, row_class, _ in ROWS:
        old = WIDENED_BEFORE.get(row_class)
        still_named = row_class not in names or (
            old is not None and re.search(old, text) is not None
        )
        got = row_class if still_named else SECOND_CAUSE.get(lane, "other")
        result[got] = result.get(got, 0) + 1
    return result


def test_friction_other_class_control_rules_as_they_stood_leave_the_rows_as_other() -> (
    None
):
    """The control: without the new rules the rows that only they name stay ``other``, so the reader alone does not move the count."""
    before = {key: int(v["occurrences"]) for key, v in roll(rules_before()).items()}
    assert before == landed({*NEW_CLASSES, *WIDENED_BEFORE})
    assert before["other"] >= 12
    assert sum(before.values()) == len(ROWS)


def test_friction_other_class_each_new_class_and_widened_pattern_names_only_its_own_rows() -> (
    None
):
    """A rule that matched another row's text would steal it; remove one class (or revert one pattern) and only its rows leave it."""
    for name in (*NEW_CLASSES, *WIDENED_BEFORE):
        rules = [r for r in shipped_rules() if r.name != name]
        old_pattern = WIDENED_BEFORE.get(name)
        if old_pattern is not None:
            rules.append(
                next(r for r in shipped_rules() if r.name == name).model_copy(
                    update={"pattern": old_pattern}
                )
            )
        got = {key: int(v["occurrences"]) for key, v in roll(rules).items()}
        assert got == landed({name}), name


def test_friction_other_class_every_new_rule_names_an_owner() -> None:
    for rule in shipped_rules():
        if rule.name in NEW_CLASSES:
            assert len(rule.owner) > 40, rule.name
