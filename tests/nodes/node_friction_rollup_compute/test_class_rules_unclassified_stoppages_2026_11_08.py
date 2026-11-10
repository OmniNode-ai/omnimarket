# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The class rules for the 12 FRICTION rows of 2026-11-08 that the 41 shipped rules left unclassified (OMN-22879).

Each row below is the text of a real ledger row (the cells the rollup reads: cost where the row had one, then the text).
With the shipped rules every one names its class; the control runs the same rows through the 41 rules as they stood and the
rollup reads all 12 as ``unclassified``. The cost of the row that carried no cost cell is read from its own lane-hours
estimate, not from the age of a snapshot its prose mentions (390 hours old, which read as 23,400 lane-minutes).
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
from omnimarket.nodes.node_friction_rollup_compute.handlers.friction_rollup_core import (
    COST_DURATION_PATTERN,
    parse_cost_minutes,
)
from omnimarket.nodes.node_friction_rollup_compute.handlers.handler_friction_rollup import (
    HandlerFrictionRollup,
)

pytestmark = pytest.mark.unit

OVERLAY = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_friction_rollup_compute/overlay.example.yaml"
)
NOW = datetime(2026, 11, 8, 18, 0, 0, tzinfo=UTC)

# The rules the overlay carried before this change: every rule from the first "linear-api-rate-limited" on is new, and the
# two widened patterns are put back to the text they had.
NEW_CLASSES = (
    "linear-api-rate-limited",
    "brief-names-workflow-file-absent-at-dev-tip",
    "lab-host-hook-test-fails-on-unmodified-base",
    "lab-lane-has-no-signing-identity",
    "prrp-snapshot-stale-on-lab-host",
    "ilo-redfish-logs-verb-reads-links-as-fields",
    "harness-delegate-fixed-call-budget-kills-long-run",
)
WIDENED_BEFORE = {
    "baselined-script-ratchet-refused-fix": "(?:shrink-only ratchet|baselined script)",
    "bus-state-read-unavailable-to-lane": "(?:No tool reads|bus live read failed)",
}

# (lane, cost cell or "", text, the class the shipped rules must name, cost minutes the rollup must read)
ROWS: tuple[tuple[str, str, str, str, float], ...] = (
    (
        "zeroflip-0815",
        "est ~0.2 lane-hours (own Linear reads, six concurrent, 1259 of 1723 returned InfraRateLimitedError)",
        "zeroflip-0815 read the live state of about 2600 contract tickets through the dod_verify tracker with six "
        "concurrent reads and hit the Linear rate limit; the reader and the verify scan were stopped by pid; other "
        "Linear consumers may have seen the limit briefly. Next time one read at a time.",
        "linear-api-rate-limited",
        12.0,
    ),
    (
        "lab-fill-omn22996-11080850",
        "",
        "The canonical-file-shape ratchet refuses any growth of scripts/deploy-agent/deploy_agent/agent.py (baselined, "
        "2232 code lines, docstrings counted), so a dev lane return-to-dev idle tick cannot be wired into the deploy "
        "agent; four wirings cost about 25 minutes of lane time and the smallest still read 2251; est ~1 lane-hours "
        "per attempt until the agent idle ticks have a canonical node home",
        "baselined-script-ratchet-refused-fix",
        60.0,
    ),
    (
        "morning-triage-adjudication",
        "~5 lane-minutes",
        "Linear API returned HTTP 429 on every read (linear_sprint_project x3, linear_issues sprint x1, workspace-48h "
        "x2); sprint scope never resolved. Related open CLAIM: OMN-22866 lane linear429-b-0920.",
        "linear-api-rate-limited",
        5.0,
    ),
    (
        "ground-state-report",
        "~5 lane-minutes",
        "Linear returned HTTP 429 on the first three reads of the run (sprint project, label, committed-class list); a "
        "retry after the non-Linear probes succeeded. Separately, the PR watcher inventory was UNREADABLE (snapshot "
        "exceeds 16 MiB), so the open-PR census is empty.",
        "linear-api-rate-limited",
        5.0,
    ),
    (
        "plan-reconcile",
        "~10 lane-minutes",
        "pull_merge_state can resolve a PR number to a commit for a different PR or ticket (repo-a#1108 "
        "returned a #1113 subject, repo-b#1619 returned a #1618 subject, omnimarket#2280 named OMN-17726); "
        "onex_change_control has no canonical clone, so OCC evidence merges are unreadable",
        "bus-state-read-unavailable-to-lane",
        10.0,
    ),
    (
        "dropped-work",
        "~10 lane-minutes",
        "Brief names omnibase_infra runtime-rebuild-trigger.yml for the deploy-agent age; it does not exist at dev tip "
        "and the substitutes (release.yml, deliver-dev-candidate-to-staging.yml) had no runs in the watcher window, so "
        "deploy-agent age stays UNKNOWN every morning until the brief names a real workflow.",
        "brief-names-workflow-file-absent-at-dev-tip",
        10.0,
    ),
    (
        "session-goal",
        "~3 lane-minutes",
        "pr_handoff refused exit 5: onex scm pr-view --source bus live read of repo-c#1687 comments "
        "failed after 152s",
        "bus-state-read-unavailable-to-lane",
        3.0,
    ),
    (
        "lab-fill-omn22387-11081430",
        "~5 min",
        "operator-waiting-tick hook test test_the_real_inbox_verb_drops_an_acked_ask_and_the_reports_are_not_asks "
        "fails 1 of 14 on host h051 on unmodified origin/main, so hook-preflight blocks every commit from a lane there",
        "lab-host-hook-test-fails-on-unmodified-base",
        5.0,
    ),
    (
        "dlg-23171-1450",
        "est ~1 lane-hours per blocked lane (two lanes so far)",
        "repo-d ruleset 26703219 (SX-INC-2026-001) requires verified signatures, and a lab-host lane has no "
        "signing identity, so git push and the contents API both refuse (GH013); the lane's finished commit cc1920a7 "
        "stays unpushed",
        "lab-lane-has-no-signing-identity",
        60.0,
    ),
    (
        "dlg-23172-1450",
        "",
        "The private-repo-runner-placement hook could not run on lab host h202 because the lane cannot read repository "
        "visibility from GitHub and the local PRRP variables snapshot was captured 2026-10-23T08:19:13Z, 390 hours old "
        "against a 24 hour limit; hook-preflight exited BLOCKED, so the 7-file change could not be "
        "committed, with no branch and no pull request; est ~0.5 lane-hours lost; fix is to refresh the PRRP snapshot "
        "on every lab host with onex-prrp-snapshot-publish",
        "prrp-snapshot-stale-on-lab-host",
        30.0,
    ),
    (
        "ilo-overlay-1640",
        "~15 lane-minutes",
        "node_ilo_redfish_effect logs verb returns 18 IML entries with every field null on iLO 4 v2.82 because the IML "
        "Entries collection holds only links in Members and read_logs reads inline fields; the overlay journal_dir "
        "cannot be a home path (overlays ci rejects it, the reader does not expand a tilde); and the pr-create "
        "duplicate guard matched overlays#61 on a shared pins file and four title words for a different change",
        "ilo-redfish-logs-verb-reads-links-as-fields",
        15.0,
    ),
    (
        "mvp-codex-review-1720",
        "~10 lane-minutes",
        "A codex hostile plan review through harness-delegate (task class code_review, effort high, 48 KB plan plus "
        "read-only code verification) ran into the class's fixed 600 s call budget, which cannot be raised, and the run "
        "(4d47ce84) was killed with an empty answer.md: codex exec writes its answer only at the end, so ten minutes of "
        "reading produced nothing.",
        "harness-delegate-fixed-call-budget-kills-long-run",
        10.0,
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


def ledger_text(rows: tuple[tuple[str, str, str, str, float], ...] = ROWS) -> str:
    lines = []
    for index, (lane, cost, text, _, _) in enumerate(rows):
        stamp = (NOW - timedelta(hours=1, minutes=index)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cost_cell = f"cost={cost} | " if cost else ""
        lines.append(
            f"{stamp} | FRICTION | lane={lane} | ticket=OMN-22879 | {cost_cell}req=x | via=bus:h200 | {text}"
        )
    return "\n".join(lines) + "\n"


def roll(rules: list[ModelFrictionClassRule]) -> dict[str, dict[str, float]]:
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
    report = HandlerFrictionRollup().handle(request).report
    return {
        item["class"]: {
            "occurrences": item["occurrences"],
            "cost_minutes": item["cost_minutes"],
        }
        for item in report["classes"]
    }


def expected_by_class() -> dict[str, dict[str, float]]:
    expected: dict[str, dict[str, float]] = {}
    for _, _, _, name, minutes in ROWS:
        entry = expected.setdefault(name, {"occurrences": 0, "cost_minutes": 0.0})
        entry["occurrences"] += 1
        entry["cost_minutes"] += minutes
    return expected


def test_shipped_rules_name_each_of_the_twelve_rows() -> None:
    classes = roll(shipped_rules())
    assert "unclassified" not in classes
    assert classes == expected_by_class()


def test_control_rules_as_they_stood_leave_all_twelve_unclassified() -> None:
    before = rules_before()
    assert len(before) == len(shipped_rules()) - len(NEW_CLASSES)
    classes = roll(before)
    assert classes["unclassified"]["occurrences"] == len(ROWS)
    assert set(classes) == {"unclassified"}


def test_each_new_class_and_each_widened_pattern_names_only_its_own_rows() -> None:
    """A rule that matched another row's text would steal it; remove one class and only its rows fall out."""
    expected = expected_by_class()
    for name in (*NEW_CLASSES, *WIDENED_BEFORE):
        rules = [r for r in shipped_rules() if r.name != name]
        if name in WIDENED_BEFORE:
            rules.append(
                next(r for r in shipped_rules() if r.name == name).model_copy(
                    update={"pattern": WIDENED_BEFORE[name]}
                )
            )
        classes = roll(rules)
        stolen = {key: value for key, value in classes.items() if key != "unclassified"}
        own = sum(1 for _, _, _, row_class, _ in ROWS if row_class == name)
        assert classes["unclassified"]["occurrences"] == own, name
        assert name not in stolen, name
        assert sum(v["occurrences"] for v in stolen.values()) == len(ROWS) - own, name
    assert expected  # the per-class counts are asserted in the first test


def test_every_new_rule_names_an_owner() -> None:
    for rule in shipped_rules():
        if rule.name in NEW_CLASSES:
            assert len(rule.owner) > 40, rule.name


# The cost of a row that carries no cost cell.

AGE_ROW_TEXT = ROWS[9][2]


def test_a_prose_age_is_not_the_cost_of_the_row() -> None:
    """Control: the first duration in the prose is the snapshot's age, which is what read as 23,400 lane-minutes."""
    first = COST_DURATION_PATTERN.search(AGE_ROW_TEXT)
    assert first is not None
    assert first.group(0).strip() == "390 hours"
    assert parse_cost_minutes(first.group(0)) == 23400.0
    classes = roll(shipped_rules())
    assert classes["prrp-snapshot-stale-on-lab-host"]["cost_minutes"] == 30.0


def test_the_lane_hours_estimate_wins_over_an_earlier_plain_duration() -> None:
    classes = roll(shipped_rules())
    assert classes["baselined-script-ratchet-refused-fix"]["cost_minutes"] == 60.0


def test_the_twelve_rows_cost_what_their_own_figures_add_to() -> None:
    classes = roll(shipped_rules())
    total = sum(entry["cost_minutes"] for entry in classes.values())
    assert total == sum(minutes for _, _, _, _, minutes in ROWS) == 225.0


def test_a_row_with_only_plain_durations_keeps_its_first_one() -> None:
    """The change must not move rows that never mentioned lane units: the first plain duration still reads."""
    rows = (
        ("x", "", "stalled for about 25 minutes, then 2 hours of retries", "c", 25.0),
    )
    request = ModelFrictionRollupRequest(
        sources=[
            ModelFrictionRollupSource(
                display="friction_rows.md",
                kind="markdown",
                text=ledger_text(rows),
            )
        ],
        since=(NOW - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        until=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        generated_at=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        bucket_hours=2,
        umbrella_tickets=[],
        aliases=[],
    )
    report = HandlerFrictionRollup().handle(request).report
    assert report["totals"]["cost_minutes"] == 25.0
