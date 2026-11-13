# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cell-by-cell parity of the node with the unit it replaces, on a recorded day (OMN-22987).

The expected output in ``fixtures/recorded_day_unit_output.json`` was recorded by running the
old unit's rollup (``build_report`` and ``render_markdown``) over the two ledger fixtures
beside it; the node must reproduce every cell of it. The planted-divergence tests show the
comparison is able to fail: each plants one change and the same comparison must name it.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.friction_rollup import (
    ModelFrictionRollupRequest,
    ModelFrictionRollupResult,
)
from omnimarket.nodes.node_friction_rollup_compute.handlers.handler_friction_rollup import (
    HandlerFrictionRollup,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
RECORDED = json.loads(
    (FIXTURES / "recorded_day_unit_output.json").read_text(encoding="utf-8")
)
SOURCE_FILES = dict(
    zip(
        RECORDED["request"]["source_displays"],
        ("recorded_day_ledger.md", "recorded_day_archive_split.md"),
        strict=True,
    )
)
HANDLER = HandlerFrictionRollup()


def recorded_request(**extra: Any) -> ModelFrictionRollupRequest:
    """The request the unit's reader would have built: live ledger first, then the archive file."""
    request = RECORDED["request"]
    sources = [
        {
            "display": display,
            "kind": "markdown",
            "text": (FIXTURES / SOURCE_FILES[display]).read_text(encoding="utf-8"),
        }
        for display in request["source_displays"]
    ]
    payload = {key: value for key, value in request.items() if key != "source_displays"}
    return ModelFrictionRollupRequest.model_validate(
        {**payload, "sources": sources, **extra}
    )


def cells(value: object, path: str = "$") -> dict[str, object]:
    """Every leaf of a JSON value keyed by its path, so a comparison names the cell that differs."""
    if isinstance(value, dict):
        found: dict[str, object] = {}
        for key, child in value.items():
            found.update(cells(child, f"{path}.{key}"))
        return found or {path: {}}
    if isinstance(value, list):
        found = {}
        for index, child in enumerate(value):
            found.update(cells(child, f"{path}[{index}]"))
        return found or {path: []}
    return {path: value}


def cell_diff(expected: object, actual: object) -> list[str]:
    """The paths whose value differs, is missing from the actual or is extra in it."""
    left, right = cells(expected), cells(actual)
    differing = [
        f"{path}: unit={left[path]!r} node={right[path]!r}"
        for path in left
        if path in right and left[path] != right[path]
    ]
    missing = [f"{path}: absent from node" for path in left if path not in right]
    extra = [f"{path}: absent from unit" for path in right if path not in left]
    return differing + missing + extra


def node_output(request: ModelFrictionRollupRequest) -> ModelFrictionRollupResult:
    return HANDLER.handle(request)


def unit_report() -> dict[str, Any]:
    report: dict[str, Any] = json.loads(RECORDED["expected"]["json_text"])
    return report


def test_recorded_day_is_not_vacuous() -> None:
    report = unit_report()
    classes = {item["class"]: item for item in report["classes"]}
    assert len(report["classes"]) >= 20
    assert report["totals"]["friction_rows"] == 265
    assert report["totals"]["rows_without_class"] == 87
    assert classes["unclassified"]["occurrences"] == 87
    assert report["duplicates_dropped"] == 4
    assert report["signals"]["delegation-failed-or-skipped"]["count"] > 0
    assert len(report["sources"]) == 2
    assert len(cells(report)) > 2500


def test_node_rollup_parity_with_unit_on_recorded_day() -> None:
    result = node_output(recorded_request())
    assert cell_diff(unit_report(), result.report) == []
    assert result.json_text == RECORDED["expected"]["json_text"]
    assert result.markdown == RECORDED["expected"]["markdown"]


PLANTED_CELL_CHANGES = {
    "a count one higher": lambda r: r["totals"].__setitem__("friction_rows", 268),
    "a class dropped": lambda r: r["classes"].pop(0),
    "the class order swapped": lambda r: r["classes"].reverse(),
    "a cost cell changed": lambda r: r["classes"][1].__setitem__("cost_minutes", 1.0),
    "a signal item dropped": lambda r: r["signals"]["delegation-failed-or-skipped"][
        "items"
    ].pop(),
    "a source summary changed": lambda r: r["sources"][0].__setitem__("rows_parsed", 0),
}


def test_planted_divergence_positive_control_is_clean() -> None:
    result = node_output(recorded_request())
    assert cell_diff(unit_report(), copy.deepcopy(result.report)) == []


@pytest.mark.parametrize("name", sorted(PLANTED_CELL_CHANGES))
def test_planted_divergence_in_a_node_cell_turns_the_comparison_red(name: str) -> None:
    planted = copy.deepcopy(node_output(recorded_request()).report)
    PLANTED_CELL_CHANGES[name](planted)
    assert cell_diff(unit_report(), planted) != [], name


def test_planted_divergence_in_markdown_text_is_seen() -> None:
    markdown = node_output(recorded_request()).markdown
    planted = markdown.replace("lab-idle", "lab-idle-x", 1)
    assert planted != RECORDED["expected"]["markdown"]


def test_planted_divergence_in_the_request_turns_the_comparison_red() -> None:
    """A node run over a differing input (one source row removed) differs from the unit's record."""
    request = recorded_request()
    first = request.sources[0]
    lines = (first.text or "").splitlines()
    index = next(i for i, line in enumerate(lines) if "| FRICTION |" in line)
    changed = request.model_copy(
        update={
            "sources": [
                first.model_copy(
                    update={"text": "\n".join(lines[:index] + lines[index + 1 :])}
                ),
                *request.sources[1:],
            ]
        }
    )
    assert cell_diff(unit_report(), node_output(changed).report) != []


def test_planted_divergence_by_a_class_rule_is_named_by_cell() -> None:
    """A rule that names every unclassified row moves cells: the comparison lists them."""
    planted = recorded_request(
        class_rules=[
            {"name": "planted-class", "owner": "planted owner", "pattern": "."}
        ]
    )
    diff = cell_diff(unit_report(), node_output(planted).report)
    assert diff != []
    assert any("planted-class" in line for line in diff)


def test_overlay_rules_split_only_the_unclassified_class_on_the_recorded_day() -> None:
    """With the node's overlay rules, every class the unit already named is identical cell by cell.

    The rules name rows the unit leaves ``unclassified``: that class shrinks, the new classes carry
    only those rows, and the friction rows and cost minutes of the day are conserved.
    """
    overlay = yaml.safe_load(
        (NODE_DIR / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    request = recorded_request(
        class_rules=overlay["class_rules"], class_owners=overlay.get("class_owners", [])
    )
    report = node_output(request).report
    unit = unit_report()
    unit_classes = {item["class"]: item for item in unit["classes"]}
    node_classes = {item["class"]: item for item in report["classes"]}
    assert report["totals"]["friction_rows"] == unit["totals"]["friction_rows"]
    assert report["totals"]["cost_minutes"] == unit["totals"]["cost_minutes"]
    for name, item in unit_classes.items():
        if name == "unclassified":
            continue
        # a class the unit named keeps its occurrences, cost, lanes and tickets
        for key in (
            "occurrences",
            "rows",
            "cost_minutes",
            "lanes",
            "tickets",
            "first_seen",
        ):
            assert node_classes[name][key] == item[key], (name, key)
    named_by_rules = [name for name in node_classes if name not in unit_classes]
    moved = sum(node_classes[name]["occurrences"] for name in named_by_rules)
    remaining = node_classes.get("unclassified", {"occurrences": 0})["occurrences"]
    assert moved + remaining == unit_classes["unclassified"]["occurrences"]
    assert named_by_rules == [] or moved > 0
