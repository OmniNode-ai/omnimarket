# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pin the model-call inventory: every source file that names a chat route has a row.

``docs/reference/model_call_inventory.yaml`` classifies each file under
``src/`` that names ``chat/completions`` as a real direct call, a declared
route or a mention. A real direct call carries its scope (customer or
internal), and every real call names the callers that prove it. The tests
below fail on a file with no row, on a class with no caller named, and on a
real call the direct-model-call baseline does not list.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[3]
INVENTORY = REPO_ROOT / "docs" / "reference" / "model_call_inventory.yaml"
BASELINE = REPO_ROOT / ".onex_ratchets" / "direct_model_call_baseline.yaml"
SOURCE_ROOT = REPO_ROOT / "src"
ROUTE_TOKEN = "chat/completions"

CLASSES = {"real_direct_call", "declared_route", "mention"}
SCOPES = {"customer", "internal"}
CUSTOMER_PATHS = {"delegation", "gateway", "onboarding_setup", "dashboard", "metering"}
MENTION_KINDS = {
    "docstring",
    "message_text",
    "test_fixture",
    "validator_corpus",
    "field_description",
    "log_parse",
    "test_stub",
}


def _load(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _source_files_naming_the_route() -> set[str]:
    return {
        path.relative_to(REPO_ROOT).as_posix()
        for path in SOURCE_ROOT.rglob("*.py")
        if ROUTE_TOKEN in path.read_text(encoding="utf-8")
    }


def _baseline_paths() -> set[str]:
    return {entry["path"] for entry in _load(BASELINE)["entries"]}


def _caller_problems(row: dict[str, Any], root: Path) -> list[str]:
    problems: list[str] = []
    callers = row.get("callers")
    if not isinstance(callers, list) or not callers:
        return [f"{row['path']}: a real direct call names no caller"]
    for caller in callers:
        path_text, _, needle = str(caller).partition("::")
        if not needle:
            problems.append(f"{row['path']}: caller {caller!r} has no '::<text>'")
            continue
        file = root / path_text
        if not file.is_file():
            problems.append(f"{row['path']}: caller file {path_text} is missing")
        elif needle not in file.read_text(encoding="utf-8"):
            problems.append(f"{row['path']}: {path_text} does not contain {needle!r}")
    return problems


def inventory_problems(
    document: dict[str, Any],
    *,
    files: set[str],
    baseline: set[str],
    root: Path,
) -> list[str]:
    """Every way the inventory can fail to account for the source files."""
    rows = document.get("rows", [])
    problems: list[str] = []
    paths = [row.get("path") for row in rows]
    for path in sorted(files - set(paths)):
        problems.append(f"{path}: names {ROUTE_TOKEN} and has no row")
    for path in sorted({p for p in paths if paths.count(p) > 1}):
        problems.append(f"{path}: has more than one row")
    for row in rows:
        path = row.get("path")
        klass = row.get("class")
        if path not in files:
            problems.append(f"{path}: row for a file that does not name the route")
            continue
        if klass not in CLASSES:
            problems.append(f"{path}: unknown class {klass!r}")
            continue
        if klass == "real_direct_call":
            scope = row.get("scope")
            if scope not in SCOPES:
                problems.append(f"{path}: real call with scope {scope!r}")
            if scope == "customer" and row.get("customer_path") not in CUSTOMER_PATHS:
                problems.append(f"{path}: customer call names no customer path")
            problems.extend(_caller_problems(row, root))
            if path not in baseline:
                problems.append(f"{path}: real call is not on the baseline")
        elif klass == "declared_route":
            if not row.get("sends_via"):
                problems.append(f"{path}: declared route names no sender")
        elif row.get("kind") not in MENTION_KINDS:
            problems.append(f"{path}: mention of kind {row.get('kind')!r}")
        if klass != "real_direct_call" and path in baseline:
            problems.append(f"{path}: on the baseline but not a real direct call")
    return problems


def test_inventory_exists_and_counts_match_the_rows() -> None:
    document = _load(INVENTORY)
    rows = document["rows"]
    assert document["counts"]["files"] == len(rows)
    for klass in CLASSES:
        assert document["counts"][klass] == sum(r["class"] == klass for r in rows)


def test_every_file_naming_the_chat_route_has_one_classified_row() -> None:
    problems = inventory_problems(
        _load(INVENTORY),
        files=_source_files_naming_the_route(),
        baseline=_baseline_paths(),
        root=REPO_ROOT,
    )
    assert problems == []


def test_every_real_direct_call_names_scope_and_callers_that_exist() -> None:
    rows = _load(INVENTORY)["rows"]
    real = [r for r in rows if r["class"] == "real_direct_call"]
    assert real, "the committed inventory lists no real direct call"
    problems = [p for r in real for p in _caller_problems(r, REPO_ROOT)]
    assert problems == []


def test_scan_finds_the_forty_files_the_inventory_covers() -> None:
    assert len(_source_files_naming_the_route()) == _load(INVENTORY)["counts"]["files"]


@pytest.fixture
def good() -> tuple[dict[str, Any], set[str], set[str]]:
    document = {
        "rows": [
            {
                "path": "src/a.py",
                "class": "real_direct_call",
                "scope": "customer",
                "customer_path": "delegation",
                "callers": ["src/caller.py::call_a"],
            },
            {"path": "src/b.py", "class": "declared_route", "sends_via": "src/a.py"},
            {"path": "src/c.py", "class": "mention", "kind": "docstring"},
        ]
    }
    return document, {"src/a.py", "src/b.py", "src/c.py"}, {"src/a.py"}


def _run(
    document: dict[str, Any], files: set[str], baseline: set[str], tmp_path: Path
) -> list[str]:
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "caller.py").write_text("call_a()\n", encoding="utf-8")
    return inventory_problems(document, files=files, baseline=baseline, root=tmp_path)


def test_control_a_consistent_inventory_has_no_problems(
    good: tuple[dict[str, Any], set[str], set[str]], tmp_path: Path
) -> None:
    document, files, baseline = good
    assert _run(document, files, baseline, tmp_path) == []


def test_control_a_file_with_no_row_is_refused(
    good: tuple[dict[str, Any], set[str], set[str]], tmp_path: Path
) -> None:
    document, files, baseline = good
    problems = _run(document, files | {"src/new.py"}, baseline, tmp_path)
    assert problems == ["src/new.py: names chat/completions and has no row"]


def test_control_a_real_call_with_no_caller_is_refused(
    good: tuple[dict[str, Any], set[str], set[str]], tmp_path: Path
) -> None:
    document, files, baseline = good
    broken = copy.deepcopy(document)
    broken["rows"][0]["callers"] = []
    assert _run(broken, files, baseline, tmp_path) == [
        "src/a.py: a real direct call names no caller"
    ]


def test_control_a_caller_that_does_not_name_the_file_is_refused(
    good: tuple[dict[str, Any], set[str], set[str]], tmp_path: Path
) -> None:
    document, files, baseline = good
    broken = copy.deepcopy(document)
    broken["rows"][0]["callers"] = ["src/caller.py::not_there"]
    problems = _run(broken, files, baseline, tmp_path)
    assert problems == ["src/a.py: src/caller.py does not contain 'not_there'"]


def test_control_a_real_call_off_the_baseline_is_refused(
    good: tuple[dict[str, Any], set[str], set[str]], tmp_path: Path
) -> None:
    document, files, _ = good
    assert _run(document, files, set(), tmp_path) == [
        "src/a.py: real call is not on the baseline"
    ]


def test_control_a_customer_call_with_no_customer_path_is_refused(
    good: tuple[dict[str, Any], set[str], set[str]], tmp_path: Path
) -> None:
    document, files, baseline = good
    broken = copy.deepcopy(document)
    del broken["rows"][0]["customer_path"]
    assert _run(broken, files, baseline, tmp_path) == [
        "src/a.py: customer call names no customer path"
    ]


def test_control_a_baseline_site_classified_as_a_mention_is_refused(
    good: tuple[dict[str, Any], set[str], set[str]], tmp_path: Path
) -> None:
    document, files, baseline = good
    assert _run(document, files, baseline | {"src/c.py"}, tmp_path) == [
        "src/c.py: on the baseline but not a real direct call"
    ]
