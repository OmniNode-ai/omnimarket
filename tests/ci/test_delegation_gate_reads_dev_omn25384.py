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

from pathlib import Path
from typing import cast

import pytest
import yaml

from tests.ci.test_delegation_health_check_caller_omn19451 import (
    _run_check,
    infra_tree,  # noqa: F401  (fixture: the pinned reader's files)
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
NIGHTLY = REPO_ROOT / ".github" / "workflows" / "delegation-regression-nightly.yml"
NIGHTLY_FILE = "delegation-regression-nightly.yml"
RUNTIME_FILES = ["src/omnimarket/delegation/__init__.py"]
RUN_ID = 44000000001

Leg = dict[str, object]


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


def test_the_alarm_only_leg_names_no_lane_so_the_runner_default_applies() -> None:
    """The lane name is written once, in the runner, and never repeated here."""
    from tests.delegation_golden import runner

    (leg,) = _alarm_only()
    assert leg["lane"] == "", leg
    assert runner._env_or("ONEX_E2E_LANE", "default-sentinel") == (
        "default-sentinel"
    ), "the test process must not hold a lane of its own"
    other_lanes = runner._KNOWN_LANES - {"dev"}
    assert len(other_lanes) == 1, other_lanes


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
    steps = cast("list[dict[str, object]]", _job()["steps"])
    names = [step.get("name") for step in steps]
    assert "Run Layer-2 golden-task runner (scoreboard)" in names
    assert "Run Layer-2 pytest (xfail-aware, fails on hard breaks)" in names


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
    assert "192.168." not in "\n".join(code_lines)
    overlay = cast(
        "dict[str, dict[str, dict[str, object]]]",
        yaml.safe_load(
            (REPO_ROOT / "config/ci_bus_lanes.yaml").read_text(encoding="utf-8")
        ),
    )
    dev = overlay["lanes"]["dev"]
    assert dev["security_protocol"] == "SASL_PLAINTEXT", dev
    assert dev["sasl_mechanism"] == "SCRAM-SHA-256", dev
    assert {"KAFKA_SASL_USERNAME", "KAFKA_SASL_PASSWORD"} <= set(env), env


# Section 2: replay the pinned reader over the conclusion each case gives a run.
def _check(
    failed_legs: set[str],
    infra_tree: Path,  # noqa: F811
) -> tuple[int, str, dict[str, object]]:
    red = {NIGHTLY_FILE: RUN_ID} if _run_conclusion(failed_legs) == "failure" else {}
    return _run_check(infra_tree, red=red, changed_files=RUNTIME_FILES, labels=[])


def test_failed_alarm_only_leg_with_green_dev_leg_passes_the_check(
    infra_tree: Path,  # noqa: F811
) -> None:
    (leg,) = _alarm_only()
    code, stdout, record = _check({cast("str", leg["leg"])}, infra_tree)
    assert record["runtime_affecting"] is True, record
    assert code == 0, stdout
    assert record["red_runs"] == [], record


def test_failed_dev_leg_fails_the_check(
    infra_tree: Path,  # noqa: F811
) -> None:
    (leg,) = _gating()
    code, stdout, record = _check({cast("str", leg["leg"])}, infra_tree)
    assert record["runtime_affecting"] is True, record
    assert code == 1, stdout
    assert str(RUN_ID) in stdout
    assert record["red_runs"] == [
        {"source": "delegation-regression-nightly", "run_id": str(RUN_ID)}
    ], record


def test_failed_dev_leg_still_fails_beside_a_failed_alarm_only_leg(
    infra_tree: Path,  # noqa: F811
) -> None:
    """Positive control: both legs red, the run is red and the check names it."""
    failed = {cast("str", leg["leg"]) for leg in _legs()}
    code, stdout, record = _check(failed, infra_tree)
    assert code == 1, stdout
    assert str(RUN_ID) in stdout
    assert len(cast("list[object]", record["red_runs"])) == 1, record


def test_all_green_is_a_positive_control(
    infra_tree: Path,  # noqa: F811
) -> None:
    code, stdout, record = _check(set(), infra_tree)
    assert code == 0, stdout
    assert record["red_runs"] == []
