# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The class rules for the five FRICTION rows that the split of the broad local-hooks rule left unclassified (OMN-22879).

Before the split these rows read as ``local-hooks-fail-one-per-attempt`` because their text names the pre-commit or pre-push stage;
the split (OMN-23220) leaves them with no class. Each row below is the cause-bearing text of a real ledger row (repository names
generalised, the phrase a rule reads kept as written). Two rows name the existing ``hook-preflight-minutes-per-commit`` class once its
pattern also reads the two ways they say it, and three causes get a rule of their own. The control runs the same rows through the rules
as they stood and the rollup reads every one as ``unclassified``, so the comparison is able to fail.
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

EXISTING_CLASS = "hook-preflight-minutes-per-commit"
OLD_EXISTING_PATTERN = r"hook-preflight runs \d+ to \d+ minutes per commit"
NEW_CLASSES = (
    "ticket-contract-prerequisite-push-refused-lane-cannot-mint-child",
    "lab-run-bus-placement-refused-by-host-table-limits",
    "mac-broker-login-no-write-grant-on-lab-work-unit-topic",
)

# (key, expected class, text)
ROWS: tuple[tuple[str, str, str], ...] = (
    (
        "hook-suite-on-the-mac",
        EXISTING_CLASS,
        "Every commit, merge commit and push of a controller stack branch ran the full pre-commit and pre-push suite on the Mac "
        "(5 to 15 minutes a commit, 15 to 35 minutes a push), so one re-carry of the install stack took 60 to 100 minutes, "
        "against the brief's rule that every test and pre-commit run goes to a lab host.",
    ),
    (
        "hook-count-under-load",
        EXISTING_CLASS,
        "A commit in omnimarket runs 170 pre-commit hooks that take about 8 minutes under load, and a failing hook ends the "
        "commit with no final message, so three commits and a push cost about 40 minutes.",
    ),
    (
        "contract-push-refused",
        "ticket-contract-prerequisite-push-refused-lane-cannot-mint-child",
        "The omnimarket pre-push hook ticket-contract-prerequisite refused the push of a branch (the pull request cites a ticket "
        "but carries no contracts/<ticket>.yaml) while the brief forbids that shared contract and requires a child ticket's own "
        "contract; a lane with Linear unauthorised cannot mint the child and cannot wait for one, so a finished fix stayed unpushed.",
    ),
    (
        "lab-run-placement",
        "lab-run-bus-placement-refused-by-host-table-limits",
        "onex_lab_run --via bus placed only on h106 and refused (host table job_kinds/max_weight) while h053, h201 and h101 read "
        "admitted; it took --host h053 to place.",
    ),
    (
        "mac-broker-grant",
        "mac-broker-login-no-write-grant-on-lab-work-unit-topic",
        "The Mac's broker login has no WRITE grant on onex.cmd.omnimarket.lab-work-unit-requested.v1, so onex lab-work run from "
        "this Mac was refused (ONEX_CORE_025_PERMISSION_DENIED) and the lane could not put the omnimarket PR heads on a lab host.",
    ),
)

# Texts the new patterns must leave to their own classes or to none: the same words one step off.
OTHER_TEXTS = (
    "the pre-push hook ticket-contract-prerequisite passed on the first push",
    "onex_lab_run --via bus placed on h053 and ran the tests",
    "the Mac's broker login has a WRITE grant on the lab work topic",
    "a commit ran 12 hooks in 4 seconds",
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
    assert roll(rules_as_they_stood(), (text,)) == {
        "unclassified": {"occurrences": 1, "cost_minutes": 5.0}
    }, key


def test_control_removed_the_three_new_rules_and_reverted_the_old_pattern() -> None:
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
