# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The class rules for the nine FRICTION rows written after 2026-11-08T19:46Z that the 49 shipped rules left unclassified (OMN-22879).

Each row below is the cause-bearing text of a real ledger row (repository names and home paths generalised, the phrase a rule
reads kept as written). The rows fall into seven causes: three name the existing ``prrp-snapshot-stale-on-lab-host`` class once its
pattern also reads the two ways those rows say it, and six causes get a rule of their own. The control runs the same rows through
the rules as they stood and the rollup reads every one as ``unclassified``, so the comparison is able to fail.
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
NOW = datetime(2026, 11, 9, 3, 0, 0, tzinfo=UTC)

EXISTING_CLASS = "prrp-snapshot-stale-on-lab-host"
OLD_EXISTING_PATTERN = (
    r"(?:PRRP (?:variables )?snapshot .{0,120}hours old|"
    r"private-repo-runner-placement hook could not run)"
)
NEW_CLASSES = (
    "prrp-hook-cannot-fetch-called-workflow-pinned-by-ref",
    "lane-env-pytest-debug-temproot-outside-system-temp",
    "delegation-event-spooled-relay-no-runtime-effects-container",
    "order-dependent-test-shard-base-red",
    "commit-hooks-hang-on-merge-commit-of-python-branch",
    "ticket-create-without-team-fails",
)

# (key, expected class, text)
ROWS: tuple[tuple[str, str, str], ...] = (
    (
        "prrp-no-visibility",
        EXISTING_CLASS,
        "The private-repo-runner-placement hook cannot read repository visibility from lab host h053 (raw GitHub read budget 0) "
        "and no PRRP snapshot file exists there, so hook-preflight exits 3 BLOCKED and the change cannot be committed or pushed; "
        "fix is to run onex-prrp-snapshot-publish so h053 holds a snapshot under 24 hours old.",
    ),
    (
        "prrp-no-snapshot",
        EXISTING_CLASS,
        "h051 holds no PRRP snapshot, so private-repo-runner-placement would block any commit touching .github/workflows exactly "
        "as it did four earlier lanes on h053 and h202; no work was built. Fix: run onex-prrp-snapshot-publish so the launch host "
        "holds a snapshot under 24 hours old, then relaunch from a saved patch.",
    ),
    (
        "prrp-called-workflow",
        "prrp-hook-cannot-fetch-called-workflow-pinned-by-ref",
        "On h201 the private-repo-runner-placement hook reads a private repository's visibility from the snapshot (age 6h40m) but "
        "then cannot fetch the called workflow advisory-job-gate-reusable.yml pinned at a dev-dated ref, because the lane raw "
        "GitHub read budget is 0, and it ends THE GATE DID NOT RUN with exit 2; the pin is a ref and not a full sha, so the hint "
        "to read it from a clone under the registry root does not apply.",
    ),
    (
        "temproot-zeroflip",
        "lane-env-pytest-debug-temproot-outside-system-temp",
        "the remote-lane host env sets PYTEST_DEBUG_TEMPROOT under the lane ledger ctx dir; the omnimarket commit hook (10 "
        "ticket_contract_prerequisite tests) and the ledger tests fail with it, so each commit and push needed the variable "
        "overridden to a /tmp path (hooks still ran, none skipped); third lane in a row to hit it",
    ),
    (
        "temproot-hook-preflight",
        "lane-env-pytest-debug-temproot-outside-system-temp",
        "hook-preflight fails four ledger hooks on lab host h053 because the lane env sets PYTEST_DEBUG_TEMPROOT outside the "
        "system temp dir, so the ledger write guard treats tmp_path ledgers as canonical; the lane cannot commit a merge of main.",
    ),
    (
        "relay-no-container",
        "delegation-event-spooled-relay-no-runtime-effects-container",
        'harness-delegate run a68184eb: the delegation event was not published; the dev-lane relay returned rc=70 "no '
        'runtime-effects container in omnibase-infra" and the event was spooled for retry, so the Codex plan-review run is '
        "missing from delegation_events.",
    ),
    (
        "order-dependent-shard",
        "order-dependent-test-shard-base-red",
        "Tests (Split 13/20) red on four pull requests with no change of their own in shard 13; serial run of the shard on h053 "
        "fails 32 delegation and projection tests that pass alone and under xdist: an order-dependent base red; causing base "
        "change not identified (job logs not in the cache, run-view 404)",
    ),
    (
        "merge-commit-hooks-hang",
        "commit-hooks-hang-on-merge-commit-of-python-branch",
        "A merge commit of a branch with Python changes into the installed Mac plugin record hung in the repository's commit "
        "hooks for over 9 minutes twice (the first merge of a contract-only diff passed in seconds), so the install-record merge "
        "commits for two pull requests were built with git commit-tree after a merge --no-commit.",
    ),
    (
        "ticket-create-team",
        "ticket-create-without-team-fails",
        "onex ticket create without --team failed with operation_failed three times after the guard passed and worked with "
        "--team set; section 9 of the create-ticket skill does not pass --team.",
    ),
)

# Texts the new patterns must leave to their own classes or to none: the same words one step off.
OTHER_TEXTS = (
    "a commit hook hung for 9 minutes on a pull request branch in CI",
    "onex ticket create worked with --team set",
    "the delegation event was published through the relay",
    "Tests (Split 13/20) red on a pull request because its own change broke a test",
    "pytest ran with the default temp root",
    "the private-repo-runner-placement hook passed",
)


def shipped_rules() -> list[ModelFrictionClassRule]:
    data = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))
    return [ModelFrictionClassRule.model_validate(r) for r in data["class_rules"]]


def rules_as_they_stood() -> list[ModelFrictionClassRule]:
    stood = []
    for rule in shipped_rules():
        if rule.name in NEW_CLASSES:
            continue
        if rule.name == EXISTING_CLASS:
            rule = rule.model_copy(update={"pattern": OLD_EXISTING_PATTERN})
        stood.append(rule)
    return stood


def ledger_text(texts: tuple[str, ...]) -> str:
    lines = []
    for index, text in enumerate(texts):
        lines.append(
            f"2026-11-09T00:{index % 60:02d}:12Z | FRICTION | lane=lane-{index} | principal=p | ticket=OMN-22879 | "
            f"cost=5m | req=r{index} | via=bus:h200 | {text}"
        )
    return "\n".join(lines) + "\n"


def roll(
    rules: list[ModelFrictionClassRule], texts: tuple[str, ...]
) -> dict[str, dict[str, float]]:
    request = ModelFrictionRollupRequest(
        sources=[
            ModelFrictionRollupSource(
                display="friction-rows.md",
                kind="markdown",
                text=ledger_text(texts),
            )
        ],
        since=(NOW - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        until=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        generated_at=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        bucket_hours=2,
        umbrella_tickets=[],
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


@pytest.mark.parametrize(("key", "expected", "text"), ROWS)
def test_shipped_rules_name_each_row(key: str, expected: str, text: str) -> None:
    assert roll(shipped_rules(), (text,)) == {
        expected: {"occurrences": 1, "cost_minutes": 5.0}
    }, key


@pytest.mark.parametrize(("key", "expected", "text"), ROWS)
def test_control_rules_as_they_stood_leave_each_row_unclassified(
    key: str, expected: str, text: str
) -> None:
    # The shard row also reads "run-view 404", which the job-log rule widened since; the shipped order-dependent rule sits above it.
    stood_as = (
        "ci-job-log-unreadable-from-the-mirror"
        if key == "order-dependent-shard"
        else "unclassified"
    )
    assert roll(rules_as_they_stood(), (text,)) == {
        stood_as: {"occurrences": 1, "cost_minutes": 5.0}
    }, key


def test_control_removed_the_six_new_rules_and_reverted_the_old_pattern() -> None:
    assert len(rules_as_they_stood()) == len(shipped_rules()) - len(NEW_CLASSES)
    assert {r.name for r in shipped_rules()} >= set(NEW_CLASSES)


def test_the_whole_batch_has_no_unclassified_row_and_the_expected_counts() -> None:
    classes = roll(shipped_rules(), tuple(text for _, _, text in ROWS))
    expected: dict[str, int] = {}
    for _, name, _ in ROWS:
        expected[name] = expected.get(name, 0) + 1
    assert "unclassified" not in classes
    assert {name: int(v["occurrences"]) for name, v in classes.items()} == expected


def test_the_patterns_leave_near_miss_rows_unclassified() -> None:
    classes = roll(shipped_rules(), OTHER_TEXTS)
    assert classes == {
        "unclassified": {
            "occurrences": len(OTHER_TEXTS),
            "cost_minutes": 5.0 * len(OTHER_TEXTS),
        }
    }


@pytest.mark.parametrize("name", NEW_CLASSES)
def test_each_new_rule_names_an_owner(name: str) -> None:
    rule = next(r for r in shipped_rules() if r.name == name)
    assert len(rule.owner) > 40
