# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor row ``stoppage:local-hooks-fail-one-per-attempt`` read 8 rows and 214 lane-minutes; the rows were 8 causes (OMN-23220).

The class meant one thing: the local hooks failed one at a time, a round per failure. Its rule also matched any row
that named the ``pre-commit`` or ``pre-push`` stage, so a slow hook set, a hook that cannot read the pull request, a
host table that fails validation and a fix worker's push all read as that one stoppage. Each row below is the text of a
real ledger row of 2026-11-07T21:01Z to 2026-11-08T15:21Z (the part the rules read: the writer, request and bus cells are left out and
the names of private repositories and lab hosts are replaced by a description). The control runs the same rows through the rule as it stood and every one reads as the one class.

A second fault sat under the same rows: the inline cost of a row with no ``cost`` cell was the first duration in its
text, so ``390 hours old against a 24 hour limit`` read as 23,400 lane-minutes of friction. The cost is now the first
duration written as lane time or with an estimate marker.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.friction_rollup import (
    ModelFrictionClassRule,
    ModelFrictionRollupRequest,
    ModelFrictionRollupSource,
)
from omnimarket.nodes.node_friction_rollup_compute.handlers import (
    friction_rollup_core,
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
BROAD = "local-hooks-fail-one-per-attempt"
# The pattern of that rule as origin/dev holds it.
BROAD_PATTERN_BEFORE = "(?:pre-push|pre-commit|hook round|commit attempts|failing hooks|release-identity hook|mixed-category hook)"

# (stamp, lane, the row's cells after lane, the class the shipped rules must name)
ROWS: tuple[tuple[str, str, str, str], ...] = (
    (
        "2026-11-07T21:01:21Z",
        "mkt-fix6-2000",
        "existing=OMN-22478 | est ~0.6 lane-hours lost; (OMN-22478) | two of this lane's fixes (omnimarket#5294, omnimarket#5326) duplicated commits the landing controller's fix workers pushed to the same branch during this lane's roughly 9-minute pre-push hook run, so both pushes were rejected as stale; fix workers write no pr_claim_registry claim, so a lane cannot see them; the lane GitHub read budget of 8 was spent on CI logs by 20:07Z, leaving omnimarket#5150 and omnimarket#5342 causes unread",
        "lane-github-read-budget-spent",
    ),
    (
        "2026-11-08T00:21:22Z",
        "unclass-d-2345",
        "existing=OMN-22865 | cost=~10 lane-minutes | hook-preflight exit 3 BLOCKED on the first commit of a new branch: it runs the repository's pre-push hook ticket-contract-prerequisite, which reads the pushed head and so cannot pass before the commit that adds contracts/<ticket>.yaml exists; every commit-stage hook passed; the lane committed with plain git commit (all hooks ran) against the brief's stop-on-BLOCKED instruction",
        "hook-preflight-push-hook-before-first-commit",
    ),
    (
        "2026-11-08T05:50:41Z",
        "val-orch-0555",
        "ticket=OMN-23078 | hook-preflight at its default --stage all runs the pre-push ticket-contract-prerequisite hook against the staged tree before the first commit, where HEAD cannot yet carry contracts/OMN-23078.yaml, so it reports BLOCKED rounds-exhausted for a change that is correct; ran --stage commit (rc 0, all commit hooks passed), committed, and the push-stage hooks ran in git push (rc 0). Separately onex_lab_run --via bus refused placement because the lab_hosts overlay fails validation on hosts.7.ilo_address and hosts.8.ilo_address, and the ssh fallback found no host, so heavy tests ran on the launching host itself. cost ~20 lane-minutes",
        "lab-run-bus-unit-no-receipt",
    ),
    (
        "2026-11-08T08:01:07Z",
        "estimator-live-b0740",
        "ticket=OMN-23120 | cost=~15 lane-minutes | Two papercuts: the lab placement command exits 69 on the bus path and raises HostTableOutsideRootsError on the ssh path from a lab host (host table under the overlay store is outside the declared roots), so the focused node tests ran directly on the lab host the lane was on; and hook-preflight runs the pre-push ticket-contract hook on staged-but-uncommitted files, so it cannot pass before the first commit of a contract-only change.",
        "lab-host-table-unusable-for-lab-run",
    ),
    (
        "2026-11-08T09:22:00Z",
        "gitmut-0815",
        "existing=OMN-22880 | ticket=OMN-22880 | cost=~1.3 lane-hours | The repository's commit-stage pre-commit hooks (about 80, run one after another) did not finish in 25 minutes on a lab host at load average 22; the preflight run passed all of them in about 4 rounds, but the commit itself was stopped by the 25-minute limit of a detached command (and by the 10-minute limit of a foreground call before it), so the finished fix has no commit and no push. The staged change is saved as a patch under the lane's reports directory.",
        "commit-stage-hooks-exceed-the-command-time-limit",
    ),
    (
        "2026-11-08T13:17:25Z",
        "labcheck-d-1225",
        "cost=~12 lane-minutes | existing=OMN-22950 | the lab placement command refused placement (exit 75) because the lab_hosts overlay failed validation on ilo_address of two hosts, so the guard suites ran on the launching host itself; the pre-commit golden-chain gate refused node-local tests and cost one 8-minute hook round",
        "local-hooks-fail-one-per-attempt",
    ),
    (
        "2026-11-08T14:18:35Z",
        "lab-fill-omn23172-11081350",
        "cost=~0.3 lane-hours; hook-preflight stopped the lane outcome=blocked with the work unpushed | existing=OMN-23172 | The private-repo-runner-placement pre-commit hook (OMN-18205) cannot resolve a private repository visibility from a lane: the GitHub read is refused (raw GitHub read budget=0) and no PRRP_VARIABLES_SNAPSHOT exists on host h053, so hook-preflight exits 3 BLOCKED on any private-repository change that touches a workflow, and the lane cannot commit. Fix: publish the snapshot with onex-prrp-snapshot-publish to the lab hosts, or have the hook read visibility from the local repo-state mirror.",
        "private-repo-runner-placement-hook-cannot-read-visibility",
    ),
    (
        "2026-11-08T15:21:05Z",
        "publisher-update-1450",
        "ticket=OMN-23081 | cost=25m | The install branch omn-23081-joins-install cannot be pushed (pre-push ticket-contract hook refuses an old merge commit citing OMN-23119 with no contracts/OMN-23119.yaml), and lab-run needs a sha on an origin branch even with --transport push, so the merged install head could not be tested on the lab; also hp-gen8, the serving host, went offline in tailscale at about 14:50Z so the served floor could not be read after the install.",
        "pre-push-ticket-contract-hook-refuses-old-merge-commit",
    ),
)
NEW_NAMES = {
    "hook-preflight-push-hook-before-first-commit",
    "commit-stage-hooks-exceed-the-command-time-limit",
    "private-repo-runner-placement-hook-cannot-read-visibility",
    "pre-push-ticket-contract-hook-refuses-old-merge-commit",
}

# the row that put 23,400 lane-minutes under the floor row: an age, a limit and then its own estimate
AGE_ROW = (
    "2026-11-08T15:09:27Z",
    "dlg-23172-1450",
    "ticket=OMN-23172 | The private-repo-runner-placement hook could not run on lab host a lab host because the lane cannot read repository visibility from GitHub and the local PRRP variables snapshot was captured 2026-10-23T08:19:13Z, 390 hours old against a 24 hour limit; hook-preflight exited BLOCKED, so the 7-file private-repository change could not be committed, with no branch and no pull request; est ~0.5 lane-hours lost; fix is to refresh the PRRP snapshot on every lab host with onex-prrp-snapshot-publish",
)


def shipped_rules() -> list[ModelFrictionClassRule]:
    document = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))
    return [ModelFrictionClassRule(**item) for item in document["class_rules"]]


def rules_before() -> list[ModelFrictionClassRule]:
    """The shipped rules as they stood: the broad pattern, none of the four rules named for these rows."""
    out = []
    for rule in shipped_rules():
        if rule.name in NEW_NAMES:
            continue
        if rule.name == BROAD:
            rule = rule.model_copy(update={"pattern": BROAD_PATTERN_BEFORE})
        out.append(rule)
    return out


def ledger_text(rows: tuple[tuple[str, ...], ...]) -> str:
    return (
        "\n".join(
            f"{stamp} | FRICTION | lane={lane} | {cells}"
            for stamp, lane, cells, *_ in rows
        )
        + "\n"
    )


def roll(
    rules: list[ModelFrictionClassRule], rows: tuple[tuple[str, ...], ...] = ROWS
) -> dict[str, Any]:
    request = ModelFrictionRollupRequest(
        sources=[
            ModelFrictionRollupSource(
                display="ROLLING_WORK_LEDGER.md",
                kind="markdown",
                text=ledger_text(rows),
            )
        ],
        since=(NOW - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        until=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        generated_at=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        bucket_hours=2,
        umbrella_tickets=["OMN-17427"],
        aliases=[],
        class_rules=rules,
    )
    return HandlerFrictionRollup().handle(request).report


def lane_classes(report: dict[str, Any]) -> dict[str, str]:
    return {
        lane: str(item["class"]) for item in report["classes"] for lane in item["lanes"]
    }


def test_the_eight_rows_read_as_their_own_causes_not_as_one_hooks_stoppage() -> None:
    got = lane_classes(roll(shipped_rules()))
    assert got == {lane: name for _, lane, _, name in ROWS}
    assert "unclassified" not in got.values()


def test_only_the_row_that_names_a_hook_round_stays_in_the_hooks_class() -> None:
    report = roll(shipped_rules())
    item = {i["class"]: i for i in report["classes"]}[BROAD]
    assert item["lanes"] == ["labcheck-d-1225"]
    assert item["rows"] == 1


def test_control_the_rule_as_it_stood_files_all_eight_rows_under_the_one_class() -> (
    None
):
    """Fails on origin/dev's rule set unless the broad pattern is the only thing the rows are read through."""
    report = roll(rules_before())
    item = {i["class"]: i for i in report["classes"]}[BROAD]
    assert item["rows"] == len(ROWS)
    assert set(item["lanes"]) == {lane for _, lane, _, _ in ROWS}


def test_each_new_rule_names_only_its_own_row() -> None:
    for name in NEW_NAMES:
        others = [
            r for r in shipped_rules() if r.name not in NEW_NAMES or r.name == name
        ]
        got = lane_classes(roll(others))
        own = {lane for _, lane, _, expected in ROWS if expected == name}
        assert {lane for lane, cls in got.items() if cls == name} == own, name


def minutes_of(report: dict[str, Any]) -> float:
    return float(report["totals"]["cost_minutes"])


def test_an_age_and_a_limit_in_the_text_are_not_the_cost_of_the_row() -> None:
    report = roll(shipped_rules(), (AGE_ROW,))
    assert minutes_of(report) == 30.0  # est ~0.5 lane-hours


def test_a_row_with_only_bare_durations_still_reads_its_first() -> None:
    row = (
        "2026-11-08T15:09:27Z",
        "bare-1",
        "ticket=OMN-23172 | lost 20 minutes on a 24 hour box",
    )
    assert minutes_of(roll(shipped_rules(), (row,))) == 20.0


def test_control_the_first_duration_of_any_kind_reads_the_age_as_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails on the old reader: with the first-match reading restored the same row costs 390 hours."""
    monkeypatch.setattr(
        friction_rollup_core,
        "_inline_cost",
        lambda text: friction_rollup_core.COST_DURATION_PATTERN.search(text),
    )
    assert minutes_of(roll(shipped_rules(), (AGE_ROW,))) == 390 * 60.0
