# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The class rules read the 11 FRICTION rows of 2026-11-09 that carry a bare ``class=other`` (OMN-22879).

The floor's ``stoppage:other`` row held 11 rows (192 lane-minutes) because the rollup applied its class rules only to a row with
no ``class=`` cell. Each row below is the text of a real ledger row (lane, cost cell, text). Through the shipped rules and the
rollup as it now reads a bare ``class=other`` every one names its real class, none is dropped, and no cost moves. The controls:
the rules as they stood leave all 11 as ``other``; a bare ``other`` no rule names stays ``other``; ``class=other`` with a
``reason`` cell stays its own class; a specific class cell is never replaced by a rule.
"""

from __future__ import annotations

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
NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)

NEW_CLASSES = (
    "lab-host-not-installed-no-ssh-key-outside-lab-run-pool",
    "ci-job-log-unreadable-from-the-mirror",
    "lab-run-placement-refused-no-host-admits-the-run",
    "criterion-needs-a-host-or-runtime-the-lane-cannot-reach",
    "fill-ticket-already-merged-or-superseded-criteria-not-reconciled",
)
# The patterns of the two classes that already owned a cause, as they stood before this change.
WIDENED_BEFORE = {
    "pr-handoff-install-first-unprovable": "(?:no installed=<host>:<revision>|installed= proof)",
    "lab-host-lacks-node-toolchain": "(?:no node, pnpm or corepack|host with no node|h\\d+ has no node)",
}

# (lane, cost cell, text, the class the shipped rules must name, lane-minutes)
ROWS: tuple[tuple[str, str, str, str, float], ...] = (
    (
        "fill-ticket-OMN-22622-20261109T0933Z",
        "~3 lane-minutes",
        "BLOCKER: AC2 needs a container roll on the mini host. It is deploy work and this lane is on a different host.",
        "criterion-needs-a-host-or-runtime-the-lane-cannot-reach",
        3.0,
    ),
    (
        "cause-stuck-omnibase_core-20261109T0954Z",
        "~7 lane-minutes",
        "BLOCKER: the failing jobs' logs are unreadable (JOB-LOG-CACHE-MISS, run-view 404). The cause cannot be confirmed, "
        "and the likely fix, a rerun, is forbidden to this lane.",
        "ci-job-log-unreadable-from-the-mirror",
        7.0,
    ),
    (
        "strictorder-0935",
        "~32 lane-minutes",
        "BLOCKER: #1810 awaits the h200 controller install; `pr-handoff` refuses without installed=<host>:<rev>.",
        "pr-handoff-install-first-unprovable",
        32.0,
    ),
    (
        "fill-ticket-OMN-22690-20261109T1005Z",
        "~5 lane-minutes",
        "BLOCKER: AC3 needs a live read of the dev runtime log, which this lane can't reach (the code fix is already merged "
        "as omnimarket#5203).",
        "criterion-needs-a-host-or-runtime-the-lane-cannot-reach",
        5.0,
    ),
    (
        "fill-ticket-OMN-22694-20261109T1005Z",
        "~4 lane-minutes",
        "BLOCKER: no lab host admitted the load run (onex_lab_run placement refused: h201 NO-UNIT-SLOT, all other hosts out "
        "of the pool)",
        "lab-run-placement-refused-no-host-admits-the-run",
        4.0,
    ),
    (
        "fill-ticket-OMN-22734-20261109T1005Z",
        "~9 lane-minutes",
        "BLOCKER: no pool host has node, and h053's Chromium can't load its shared libraries, so the 11 skipped tests and "
        "the `#machines` screenshot can't run on the lab.",
        "lab-host-lacks-node-toolchain",
        9.0,
    ),
    (
        "joblogbind-0935",
        "~59 lane-minutes",
        "BLOCKER: omnimarket#5575 has a red Tests (Split 2/20) whose log is not in the mirror, and `/omni:lab-run` returned "
        "exit 75 (no pool host free), so the cause is unread.",
        "ci-job-log-unreadable-from-the-mirror",
        59.0,
    ),
    (
        "ctxsched-0955",
        "~37 lane-minutes",
        "BLOCKER: h051 and h053 not installed: no ssh key, not in the lab-run pool, child lane refused by ticket claim",
        "lab-host-not-installed-no-ssh-key-outside-lab-run-pool",
        37.0,
    ),
    (
        "fill-ticket-OMN-22746-20261109T1038Z",
        "~4 lane-minutes",
        "BLOCKER: the work is merged as repo-a#4, and the ticket's falsifiers target a stale registry copy where "
        "freeze-gate refuses the added site.css (AC1) and net added lines (AC3, AC4).",
        "fill-ticket-already-merged-or-superseded-criteria-not-reconciled",
        4.0,
    ),
    (
        "labharness2-1020",
        "~29 lane-minutes",
        "BLOCKER: Mac install of cb9d436c1 is outside my reach, and `pr-handoff` refuses without it",
        "pr-handoff-install-first-unprovable",
        29.0,
    ),
    (
        "fill-ticket-OMN-22758-20261109T1038Z",
        "~3 lane-minutes",
        "BLOCKER: OMN-22758 is superseded by OMN-22781 (repo-b#1497); a second copy in `dev.bifrost.yaml` would be "
        "new `check-hardcoded-model-config-compute` findings (04:28Z lane's report, not re-run here).",
        "fill-ticket-already-merged-or-superseded-criteria-not-reconciled",
        3.0,
    ),
)


def shipped_rules() -> list[ModelFrictionClassRule]:
    document = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))
    return [ModelFrictionClassRule(**item) for item in document["class_rules"]]


def rules_before() -> list[ModelFrictionClassRule]:
    """The shipped rules as they stood: no new class, the two widened patterns as they were."""
    return [
        rule.model_copy(update={"pattern": WIDENED_BEFORE.get(rule.name, rule.pattern)})
        for rule in shipped_rules()
        if rule.name not in NEW_CLASSES
    ]


def ledger_text(
    rows: tuple[tuple[str, str, str, str, float], ...] = ROWS,
    class_cell: str = "class=other | ",
) -> str:
    lines = []
    for index, (lane, cost, text, _, _) in enumerate(rows):
        stamp = (NOW - timedelta(hours=1, minutes=index)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(
            f"{stamp} | FRICTION | lane={lane} | ticket=OMN-22879 | cost={cost} | {class_cell}req=x | via=bus:h200 | {text}"
        )
    return "\n".join(lines) + "\n"


def roll_report(
    rules: list[ModelFrictionClassRule],
    text: str | None = None,
) -> dict[str, object]:
    request = ModelFrictionRollupRequest(
        sources=[
            ModelFrictionRollupSource(
                display="friction_rows.md",
                kind="markdown",
                text=ledger_text() if text is None else text,
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


def roll(
    rules: list[ModelFrictionClassRule], text: str | None = None
) -> dict[str, dict[str, float]]:
    report = roll_report(rules, text)
    classes = report["classes"]
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


def test_shipped_rules_name_each_of_the_eleven_bare_other_rows() -> None:
    classes = roll(shipped_rules())
    assert "other" not in classes
    assert classes == expected_by_class()


def test_no_row_is_dropped_and_no_cost_moves() -> None:
    """Never drop a row from the count: the 11 rows and their 192 lane-minutes are the same before and after."""
    after = roll_report(shipped_rules())
    before = roll_report(rules_before())
    for report in (after, before):
        totals = report["totals"]
        assert isinstance(totals, dict)
        assert totals["friction_rows"] == len(ROWS)
        assert totals["cost_minutes"] == sum(minutes for *_, minutes in ROWS) == 192.0
    after_totals = after["totals"]
    assert isinstance(after_totals, dict)
    assert after_totals["rows_classified_by_rule"] == len(ROWS)


def test_control_rules_as_they_stood_leave_all_eleven_as_other() -> None:
    classes = roll(rules_before())
    assert classes == {"other": {"occurrences": len(ROWS), "cost_minutes": 192.0}}


# One row names two causes: the red job's log is not in the mirror and the lab-run fallback was refused. The log rule goes first;
# without it the placement rule names the row.
SECOND_CAUSE = {"joblogbind-0935": "lab-run-placement-refused-no-host-admits-the-run"}


def test_each_new_class_and_each_widened_pattern_names_only_its_own_rows() -> None:
    """A rule that matched another row's text would steal it; remove one class and only its rows leave it."""
    for name in (*NEW_CLASSES, *WIDENED_BEFORE):
        rules = [r for r in shipped_rules() if r.name != name]
        if name in WIDENED_BEFORE:
            rules.append(
                next(r for r in shipped_rules() if r.name == name).model_copy(
                    update={"pattern": WIDENED_BEFORE[name]}
                )
            )
        expected: dict[str, int] = {}
        for lane, _, _, row_class, _ in ROWS:
            landed = SECOND_CAUSE.get(lane, "other") if row_class == name else row_class
            expected[landed] = expected.get(landed, 0) + 1
        got = {key: int(v["occurrences"]) for key, v in roll(rules).items()}
        assert got == expected, name


def test_every_new_rule_names_an_owner() -> None:
    for rule in shipped_rules():
        if rule.name in NEW_CLASSES:
            assert len(rule.owner) > 40, rule.name


# The reading of a bare class=other, not of the rules.


def test_a_bare_other_that_no_rule_names_stays_other() -> None:
    rows = (("x", "~2 lane-minutes", "BLOCKER: nothing a rule knows", "other", 2.0),)
    assert roll(shipped_rules(), ledger_text(rows)) == {
        "other": {"occurrences": 1, "cost_minutes": 2.0}
    }


def test_class_other_with_a_reason_cell_keeps_its_own_class() -> None:
    """A reason cell makes ``other:<reason>`` a class of its own; a rule must not rename it."""
    text = ledger_text(ROWS[:1], class_cell="class=other | reason=host-gap | ")
    assert list(roll(shipped_rules(), text)) == ["other:host-gap"]


def test_a_specific_class_cell_is_never_replaced_by_a_rule() -> None:
    text = ledger_text(ROWS[:1], class_cell="class=deploy-gap | ")
    assert list(roll(shipped_rules(), text)) == ["deploy-gap"]


def test_a_row_with_no_class_cell_is_still_named_by_the_same_rule() -> None:
    text = ledger_text(ROWS[:1], class_cell="")
    assert list(roll(shipped_rules(), text)) == [ROWS[0][3]]
