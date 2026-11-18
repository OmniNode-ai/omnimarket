# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Delegation Health Check gates dev on a nightly run against dev (OMN-25384).

RULING 2026-11-17T14:08:49Z: the check that gates omnimarket merges into dev read
the nightly delegation regression, and that nightly measured the stability lane,
so a broken stability environment froze the dev merge queue for about 12 hours
while dev was healthy.

The check's reader lives in omnibase_infra (``scripts/ci/delegation_health_check.py``
with ``config/delegation_health_check.yaml``). It reads the newest run of
``delegation-regression-nightly.yml`` and decides on the run's CONCLUSION. This
repository enforces the ruling by what that run measures and what can turn its
conclusion red:

* the gating leg of the job measures the dev lane;
* the stability measurement is a second, alarm-only leg: ``continue-on-error``
  keeps its red out of the run's conclusion, so it can never fail the check.

Section 1 reads the workflow and needs no network. Section 2 replays the pinned
reader (the fixture mechanism of ``test_delegation_health_check_caller_omn19451.py``)
over the conclusion GitHub gives a run whose legs finished the way each case says.
"""

from __future__ import annotations

import importlib
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
import yaml

from tests.ci import test_delegation_health_check_caller_omn19451 as caller
from tests.ci.test_delegation_health_check_caller_omn19451 import _run_check

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
NIGHTLY = REPO_ROOT / ".github" / "workflows" / "delegation-regression-nightly.yml"
NIGHTLY_FILE = "delegation-regression-nightly.yml"
RUNTIME_FILES = ["src/omnimarket/delegation/__init__.py"]
RUN_ID = 44000000001
DISPATCH_TITLE = "Delegation Regression (nightly) lane=dev"

Leg = dict[str, object]


@pytest.fixture(scope="module")
def pinned_infra_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The pinned reader's files, built by the caller test's own fixture."""
    build = cast(
        "Callable[[pytest.TempPathFactory], Path]",
        vars(caller.infra_tree)["__wrapped__"],
    )
    return build(tmp_path_factory)


def _document() -> dict[object, object]:
    raw: object = yaml.safe_load(NIGHTLY.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return cast("dict[object, object]", raw)


def _job() -> dict[str, object]:
    jobs = cast("dict[str, dict[str, object]]", _document()["jobs"])
    return jobs["golden-tasks"]


def _legs() -> list[Leg]:
    strategy = cast("dict[str, dict[str, list[Leg]]]", _job()["strategy"])
    return strategy["matrix"]["include"]


def _gating() -> list[Leg]:
    return [leg for leg in _legs() if not leg["alarm_only"]]


def _alarm_only() -> list[Leg]:
    return [leg for leg in _legs() if leg["alarm_only"]]


def _run_conclusion(failed_legs: set[str]) -> str:
    """The conclusion GitHub gives a run whose matrix legs ended this way.

    A failed leg fails the run unless its job is continue-on-error; the job's
    ``continue-on-error`` is the workflow's own expression, read per leg here.
    """
    expression = _job()["continue-on-error"]
    assert expression == "${{ matrix.alarm_only }}", expression
    for leg in _legs():
        if leg["leg"] in failed_legs and not leg["alarm_only"]:
            return "failure"
    return "success"


# Section 1: what the workflow measures, no network.
def test_the_gating_leg_measures_the_dev_lane() -> None:
    assert _job()["env"] == {
        **cast("dict[str, str]", _job()["env"]),
        "ONEX_E2E_LANE": "${{ matrix.lane }}",
    }
    assert [leg["lane"] for leg in _gating()] == ["dev"]


def test_there_is_exactly_one_alarm_only_leg_and_it_is_not_the_dev_lane() -> None:
    (leg,) = _alarm_only()
    assert leg["lane"] != "dev", leg


def test_the_alarm_only_leg_names_no_lane_so_the_runner_default_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The empty string is SET, not unset: matrix lane '' renders
    ONEX_E2E_LANE=''; the runner must treat it as unset.
    """
    from tests.delegation_golden import runner

    (leg,) = _alarm_only()
    assert leg["lane"] == ""
    (other_lane,) = runner._KNOWN_LANES - {"dev"}
    monkeypatch.setenv("ONEX_E2E_LANE", "")
    try:
        reloaded = importlib.reload(runner)
        assert other_lane == reloaded._LANE
        assert reloaded._env_or("ONEX_E2E_LANE", other_lane) == other_lane
    finally:
        monkeypatch.delenv("ONEX_E2E_LANE", raising=False)
        importlib.reload(runner)


def test_a_red_alarm_only_leg_cannot_turn_the_run_red() -> None:
    assert _job()["continue-on-error"] == "${{ matrix.alarm_only }}"
    assert all(leg["alarm_only"] is True for leg in _alarm_only())
    assert all(leg["alarm_only"] is False for leg in _gating())


def test_a_failed_leg_does_not_cancel_the_other() -> None:
    strategy = cast("dict[str, object]", _job()["strategy"])
    assert strategy["fail-fast"] is False


def test_the_run_title_names_the_gating_lane_and_dispatch_takes_no_lane() -> None:
    document = _document()
    assert cast("str", document["run-name"]).endswith("lane=dev")
    triggers = cast("dict[str, object]", document.get(True, document.get("on")))
    assert "schedule" in triggers
    assert not triggers["workflow_dispatch"], "no dispatch input may retarget the lane"


def test_both_legs_run_the_same_golden_tasks_and_assertions() -> None:
    """The legs are one job: one list of steps, so the corpus and its assertions
    are the same for both. Only the matrix values differ."""
    assert len(_legs()) >= 2
    steps = cast("list[dict[str, object]]", _job()["steps"])
    names = [step.get("name") for step in steps]
    assert "Run Layer-2 golden-task runner (scoreboard)" in names
    assert "Run Layer-2 pytest (xfail-aware, fails on hard breaks)" in names
    assert [
        step.get("name") for step in steps if "matrix." in str(step.get("if", ""))
    ] == ["Alarm-only leg is red (an alarm, never a merge gate)"]


def test_each_leg_uploads_artifacts_under_its_own_name() -> None:
    """Two legs sharing one artifact name would make the second upload fail."""
    steps = cast("list[dict[str, dict[str, str]]]", _job()["steps"])
    names = [
        step["with"]["name"]
        for step in steps
        if "with" in step and "name" in step["with"]
    ]
    assert names, steps
    assert all("${{ matrix.artifact_suffix }}" in name for name in names), names
    assert len({str(leg["artifact_suffix"]) for leg in _legs()}) == len(_legs())


def test_dev_leg_reads_its_endpoints_from_the_lane_overlay() -> None:
    """No broker, host or port literal in the workflow: the dev lane's broker,
    transport and mechanism are read from config/ci_bus_lanes.yaml."""
    env = cast("dict[str, str]", _job()["env"])
    text = NIGHTLY.read_text(encoding="utf-8")
    assert not any("BOOTSTRAP" in key or "POSTGRES_HOST" in key for key in env), env
    code_lines = [
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    ]
    code = "\n".join(code_lines)
    assert not re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", code)
    assert not re.search(r"\b[\w.-]+:\d{2,5}\b", code)
    overlay = cast(
        "dict[str, dict[str, dict[str, object]]]",
        yaml.safe_load(
            (REPO_ROOT / "config/ci_bus_lanes.yaml").read_text(encoding="utf-8")
        ),
    )
    (gating,) = _gating()
    lane = cast("str", gating["lane"])
    entry = overlay["lanes"][lane]
    assert cast("str", entry["broker"]) not in text
    assert entry["security_protocol"] == "SASL_PLAINTEXT", entry
    assert entry["sasl_mechanism"] == "SCRAM-SHA-256", entry
    assert {"KAFKA_SASL_USERNAME", "KAFKA_SASL_PASSWORD"} <= set(env), env


def test_the_alarm_step_runs_on_failure_or_cancel_of_the_alarm_leg() -> None:
    """failure() is false on a timeout or cancel."""
    steps = cast("list[dict[str, object]]", _job()["steps"])
    (step,) = [
        step
        for step in steps
        if step.get("name") == "Alarm-only leg is red (an alarm, never a merge gate)"
    ]
    assert step["if"] == "(failure() || cancelled()) && matrix.alarm_only"


def test_the_run_note_names_the_three_readers_of_this_run() -> None:
    text = NIGHTLY.read_text(encoding="utf-8")
    for name in (
        "delegation_health_check.yaml",
        "deliver-dev-candidate-to-staging.yml",
        "lab_pass_gate.py",
        "KNOWN GAP",
    ):
        assert name in text


def test_legs_run_one_at_a_time_gating_first() -> None:
    strategy = cast("dict[str, object]", _job()["strategy"])
    assert strategy["max-parallel"] == 1
    assert _legs()[0]["alarm_only"] is False


# Section 2: replay the pinned reader over the conclusion each case gives a run.
def _check(
    failed_legs: set[str],
    pinned_infra_tree: Path,
) -> tuple[int, str, dict[str, object]]:
    red = {NIGHTLY_FILE: RUN_ID} if _run_conclusion(failed_legs) == "failure" else {}
    return _run_check(
        pinned_infra_tree, red=red, changed_files=RUNTIME_FILES, labels=[]
    )


def test_failed_alarm_only_leg_with_green_dev_leg_passes_the_check(
    pinned_infra_tree: Path,
) -> None:
    (leg,) = _alarm_only()
    code, stdout, record = _check({cast("str", leg["leg"])}, pinned_infra_tree)
    assert record["runtime_affecting"] is True, record
    assert code == 0, stdout
    assert record["red_runs"] == [], record


def test_failed_dev_leg_fails_the_check(
    pinned_infra_tree: Path,
) -> None:
    (leg,) = _gating()
    code, stdout, record = _check({cast("str", leg["leg"])}, pinned_infra_tree)
    assert record["runtime_affecting"] is True, record
    assert code == 1, stdout
    assert str(RUN_ID) in stdout
    assert record["red_runs"] == [
        {"source": "delegation-regression-nightly", "run_id": str(RUN_ID)}
    ], record


def test_failed_dev_leg_still_fails_beside_a_failed_alarm_only_leg(
    pinned_infra_tree: Path,
) -> None:
    """Positive control: both legs red, the run is red and the check names it."""
    failed = {cast("str", leg["leg"]) for leg in _legs()}
    code, stdout, record = _check(failed, pinned_infra_tree)
    assert code == 1, stdout
    assert str(RUN_ID) in stdout
    assert len(cast("list[object]", record["red_runs"])) == 1, record


def test_all_green_is_a_positive_control(
    pinned_infra_tree: Path,
) -> None:
    code, stdout, record = _check(set(), pinned_infra_tree)
    assert code == 0, stdout
    assert record["red_runs"] == []


def _nightly_only_tree(pinned_infra_tree: Path, tmp_path: Path, title: str) -> Path:
    """A copy of the pinned reader whose only source is the nightly, admitting
    dispatches on ``title``. The replay rows are shared by every source, so the
    other sources are dropped to leave the nightly's own verdict visible."""
    tree = shutil.copytree(pinned_infra_tree, tmp_path / "tree")
    config = tree / "config/delegation_health_check.yaml"
    document = cast("dict[str, object]", yaml.safe_load(config.read_text("utf-8")))
    sources = cast("list[dict[str, object]]", document["sources"])
    (nightly,) = [s for s in sources if s["workflow"] == NIGHTLY_FILE]
    nightly["dispatch_title_contains"] = title
    document["sources"] = [nightly]
    config.write_text(yaml.safe_dump(document), encoding="utf-8")
    return tree


def _pinned_dispatch_title(pinned_infra_tree: Path) -> str:
    """The title the pinned reader admits a dispatch on, read from its config."""
    pinned = yaml.safe_load(
        (pinned_infra_tree / "config/delegation_health_check.yaml").read_text("utf-8")
    )
    (nightly,) = [s for s in pinned["sources"] if s["workflow"] == NIGHTLY_FILE]
    return cast("str", nightly["dispatch_title_contains"])


def _dispatch_over_red_schedule() -> list[dict[str, object]]:
    return [
        {
            "id": 44000000002,
            "conclusion": "success",
            "hours_ago": 1.0,
            "event": "workflow_dispatch",
            "display_title": DISPATCH_TITLE,
        },
        {"id": 44000000001, "conclusion": "failure", "hours_ago": 20.0},
    ]


def test_a_dispatched_lane_dev_run_cannot_clear_a_red_scheduled_run_at_the_pin(
    pinned_infra_tree: Path,
    tmp_path: Path,
) -> None:
    """The pinned config admits a dispatch only on a title naming the stability
    lane, so until the omnibase_infra config change and caller pin bump land,
    only the next scheduled run can clear a red one.
    """
    pinned_title = _pinned_dispatch_title(pinned_infra_tree)
    assert pinned_title.startswith("lane="), pinned_title
    assert pinned_title not in DISPATCH_TITLE, pinned_title
    tree = _nightly_only_tree(pinned_infra_tree, tmp_path, pinned_title)
    code, stdout, record = _run_check(
        tree,
        runs=_dispatch_over_red_schedule(),
        changed_files=RUNTIME_FILES,
        labels=[],
    )
    assert code == 1, stdout
    assert record["red_runs"] == [
        {"source": "delegation-regression-nightly", "run_id": "44000000001"}
    ]


def test_a_dispatched_lane_dev_run_clears_it_once_the_reader_admits_the_title(
    pinned_infra_tree: Path,
    tmp_path: Path,
) -> None:
    """This is the change the reader's owner must land first: the same rows,
    with the reader admitting a dispatch titled for the dev lane."""
    tree = _nightly_only_tree(pinned_infra_tree, tmp_path, "lane=dev")
    code, stdout, record = _run_check(
        tree,
        runs=_dispatch_over_red_schedule(),
        changed_files=RUNTIME_FILES,
        labels=[],
    )
    assert code == 0, stdout
    assert record["red_runs"] == []


def test_a_scheduled_run_ignores_the_title(
    pinned_infra_tree: Path,
    tmp_path: Path,
) -> None:
    """Positive control: a scheduled run is admitted whatever its title says."""
    tree = _nightly_only_tree(
        pinned_infra_tree, tmp_path, _pinned_dispatch_title(pinned_infra_tree)
    )
    runs: list[dict[str, object]] = [
        {
            "id": 44000000002,
            "conclusion": "success",
            "hours_ago": 1.0,
            "display_title": DISPATCH_TITLE,
        },
        {"id": 44000000001, "conclusion": "failure", "hours_ago": 20.0},
    ]
    code, stdout, record = _run_check(
        tree, runs=runs, changed_files=RUNTIME_FILES, labels=[]
    )
    assert code == 0, stdout
    assert record["red_runs"] == []
