# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20001 - the event-registry drift check reads omniclaude at the pinned rev.

Four properties, each with its own ``-k`` falsifier:

* ``drift_reads_pinned_rev``: omniclaude ``dev`` moving with the pin unmoved
  leaves the production drift check green; moving the pin is what changes it.
* ``pin_move_requires_integration``: every omniclaude read in ``ci.yml`` names
  one pin, and the jobs that read it (including an integration step that runs
  the omniclaude-coupled tests) sit in the strict set CI Summary waits on, so
  the pin moves only in a PR whose integration suite passed against it.
* ``drift_strict_both_directions``: one mutation per direction turns the
  production drift check red; the consumer-ahead tolerance stays gone.
* ``omn13146_tests_guarded``: the two OMN-13146 tests are present, run, and
  assert the polarity they were written with.
"""

from __future__ import annotations

import ast
import copy
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.validators.event_registry_drift import (
    main as drift_main,
)
from omnimarket.validators.event_registry_drift import (
    read_pinned_omniclaude_rev,
)
from scripts.ci.ci_summary_gate import STRICT_GATE_JOBS

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
DRIFT_TEST_FILE = REPO_ROOT / "tests" / "validators" / "test_event_registry_drift.py"
DRIFT_JOB = "event-registry-drift"
INTEGRATION_STEP = "Integration suite against the pinned omniclaude (OMN-20001)"
INTEGRATION_TARGET = "tests/validators/test_event_registry_drift.py"
# ``name:`` of every ci.yml job that reads omniclaude and must gate CI Summary.
OMNICLAUDE_READING_GATES = ("Event Registry Drift", "contract-topic-graph")

_TOPICS_PY = (
    "from enum import StrEnum\n"
    "class TopicBase(StrEnum):\n"
    '    SESSION_STARTED = "onex.evt.omniclaude.session-started.v1"\n'
)
_EXTRA_TOPIC_LINE = '    EXTRA = "onex.evt.omniclaude.extra.v1"\n'
_REGISTRY_PY = (
    "EVENT_REGISTRY: dict = {\n"
    "    'session.started': EventRegistration(\n"
    "        event_type='session.started',\n"
    "        fan_out=[FanOutRule(topic_base=TopicBase.SESSION_STARTED)],\n"
    "    ),\n"
    "}\n"
)
_EXTRA_EVENT = (
    "    'extra': EventRegistration(\n"
    "        event_type='extra',\n"
    "        fan_out=[FanOutRule(topic_base=TopicBase.EXTRA)],\n"
    "    ),\n"
)
_MARKET_YAML = (
    "---\nevents:\n"
    "  session.started:\n"
    "    fan_out:\n"
    '      - topic: "onex.evt.omniclaude.session-started.v1"\n'
)
_EXTRA_MARKET_EVENT = (
    '  extra:\n    fan_out:\n      - topic: "onex.evt.omniclaude.extra.v1"\n'
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return result.stdout.strip()


def _commit_hook_files(
    omniclaude: Path, *, topics: str, registry: str, msg: str
) -> None:
    hooks = omniclaude / "src" / "omniclaude" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "topics.py").write_text(topics)
    (hooks / "event_registry.py").write_text(registry)
    _git(omniclaude, "add", "-A")
    _git(
        omniclaude,
        "-c",
        "user.name=lane",
        "-c",
        "user.email=lane@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        msg,
    )


def _with_extra_event(topics: str, registry: str) -> tuple[str, str]:
    return (
        topics + _EXTRA_TOPIC_LINE,
        registry.replace("}\n", _EXTRA_EVENT + "}\n"),
    )


def make_omniclaude(tmp_path: Path, *, source_extra_at_pin: bool = False) -> Path:
    """An omniclaude clone with tag ``v-pin`` at A and ``dev`` HEAD moved to B.

    B registers an event A does not, so a check that reads HEAD sees drift and a
    check that reads the pin does not.
    """
    omniclaude = tmp_path / "omniclaude"
    omniclaude.mkdir()
    _git(omniclaude, "init", "-q", "-b", "dev")
    topics, registry = _TOPICS_PY, _REGISTRY_PY
    if source_extra_at_pin:
        topics, registry = _with_extra_event(topics, registry)
    _commit_hook_files(omniclaude, topics=topics, registry=registry, msg="pin")
    _git(omniclaude, "tag", "v-pin")
    moved_topics, moved_registry = _with_extra_event(_TOPICS_PY, _REGISTRY_PY)
    _commit_hook_files(
        omniclaude, topics=moved_topics, registry=moved_registry, msg="dev moves"
    )
    _git(omniclaude, "tag", "v-moved")
    return omniclaude


def make_repo_root(tmp_path: Path, *, pin: str, registry_extra: bool = False) -> Path:
    """A minimal omnimarket tree: ci.yml carrying the pin, registry, baseline."""
    root = tmp_path / "omnimarket"
    (root / "src" / "omnimarket").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'omnimarket'\n")
    workflow = root / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        yaml.safe_dump(
            {
                "jobs": {
                    DRIFT_JOB: {
                        "steps": [
                            {
                                "name": "Clone omniclaude",
                                "run": "git clone --depth=1 --branch "
                                f"{pin} https://github.com/OmniNode-ai/omniclaude.git"
                                " ../omniclaude",
                            }
                        ]
                    }
                }
            }
        )
    )
    registry = root / "src/omnimarket/nodes/node_emit_daemon/registries/topics.yaml"
    registry.parent.mkdir(parents=True)
    registry.write_text(_MARKET_YAML + (_EXTRA_MARKET_EVENT if registry_extra else ""))
    baseline = root / "scripts/validation/event_registry_drift_baseline.txt"
    baseline.parent.mkdir(parents=True)
    baseline.write_text("# no carve-outs\n")
    return root


def run_drift(
    root: Path, omniclaude: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str]:
    code = drift_main(["--repo-root", str(root), "--omniclaude-root", str(omniclaude)])
    return code, capsys.readouterr().err


# =============================================================================
# AC1: the drift check reads the pinned rev
# =============================================================================


def test_drift_reads_pinned_rev_dev_moving_leaves_check_green(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    omniclaude = make_omniclaude(tmp_path)
    root = make_repo_root(tmp_path, pin="v-pin")
    head = _git(omniclaude, "rev-parse", "HEAD")
    pinned = _git(omniclaude, "rev-parse", "v-pin^{commit}")
    assert head != pinned, (
        "positive control: omniclaude dev must have moved past the pin"
    )

    code, err = run_drift(root, omniclaude, capsys)

    assert code == 0, f"unmoved pin must stay green while dev moves, got: {err}"


def test_drift_reads_pinned_rev_moving_the_pin_is_what_changes_the_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    omniclaude = make_omniclaude(tmp_path)
    root = make_repo_root(tmp_path, pin="v-moved")

    code, err = run_drift(root, omniclaude, capsys)

    assert code == 1, "positive control: a pin at the moved rev must see the drift"
    assert "onex.evt.omniclaude.extra.v1" in err


def test_drift_reads_pinned_rev_ignores_dirty_working_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    omniclaude = make_omniclaude(tmp_path)
    root = make_repo_root(tmp_path, pin="v-pin")
    (omniclaude / "src/omniclaude/hooks/topics.py").write_text(
        _TOPICS_PY + '    DIRTY = "onex.evt.omniclaude.dirty.v1"\n'
    )

    code, err = run_drift(root, omniclaude, capsys)

    assert code == 0, f"an uncommitted edit in the checkout reached the check: {err}"


def test_drift_reads_pinned_rev_missing_tag_never_falls_back_to_head(
    tmp_path: Path,
) -> None:
    omniclaude = make_omniclaude(tmp_path)
    root = make_repo_root(tmp_path, pin="v-absent")

    with pytest.raises(FileNotFoundError, match="pinned revision 'v-absent'"):
        drift_main(["--repo-root", str(root), "--omniclaude-root", str(omniclaude)])


def test_drift_reads_pinned_rev_pin_is_the_ci_clone_tag() -> None:
    pin = read_pinned_omniclaude_rev(REPO_ROOT)
    assert re.fullmatch(r"v\d+\.\d+\.\d+", pin), pin
    assert f"--branch {pin} " in CI_WORKFLOW.read_text()


# =============================================================================
# AC2: the pin moves only after the integration suite passes against it
# =============================================================================

_OMNICLAUDE_CLONE = re.compile(
    r"git clone\b[^\n]*?--branch\s+(?P<ref>\S+)[^\n]*"
    r"github\.com/OmniNode-ai/omniclaude\.git"
)


def omniclaude_pins(workflow: dict[str, Any]) -> set[str]:
    """Every ref ci.yml reads omniclaude at, from clone steps and checkouts."""
    pins: set[str] = set()
    for job in (workflow.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            run = str(step.get("run", ""))
            for match in _OMNICLAUDE_CLONE.finditer(run):
                pins.add(match.group("ref"))
            with_ = step.get("with") or {}
            if with_.get("repository") == "OmniNode-ai/omniclaude":
                pins.add(str(with_.get("ref", "")))
    return pins


def pin_move_violations(
    workflow: dict[str, Any], strict_gates: tuple[str, ...]
) -> list[str]:
    violations: list[str] = []
    pins = omniclaude_pins(workflow)
    if len(pins) != 1:
        violations.append(
            f"ci.yml reads omniclaude at {sorted(pins)}; a pin move must change "
            "every omniclaude read in one PR, so there is exactly one pin"
        )
    jobs = workflow.get("jobs") or {}
    drift_job = jobs.get(DRIFT_JOB)
    steps = (drift_job or {}).get("steps") or []
    names = [step.get("name") for step in steps]
    if INTEGRATION_STEP not in names:
        violations.append(
            f"job {DRIFT_JOB} has no step {INTEGRATION_STEP!r}; a pin move would "
            "merge without the omniclaude-coupled tests running against it"
        )
    else:
        step = steps[names.index(INTEGRATION_STEP)]
        run = str(step.get("run", ""))
        if "Clone omniclaude" not in names or names.index(
            INTEGRATION_STEP
        ) < names.index("Clone omniclaude"):
            violations.append(
                "the integration step must run after omniclaude is cloned"
            )
        if "pytest" not in run or INTEGRATION_TARGET not in run:
            violations.append(
                f"step {INTEGRATION_STEP!r} must run pytest on {INTEGRATION_TARGET}"
            )
    for gate in OMNICLAUDE_READING_GATES:
        if gate not in strict_gates:
            violations.append(
                f"{gate!r} reads omniclaude at the pin but is not in the strict "
                "set CI Summary waits on"
            )
    return violations


def _load_workflow() -> dict[str, Any]:
    workflow: dict[str, Any] = yaml.safe_load(CI_WORKFLOW.read_text())
    return workflow


def test_pin_move_requires_integration_holds_on_the_live_workflow() -> None:
    assert pin_move_violations(_load_workflow(), STRICT_GATE_JOBS) == []


def test_pin_move_requires_integration_flags_a_partial_pin_move() -> None:
    workflow = copy.deepcopy(_load_workflow())
    for job in workflow["jobs"].values():
        for step in job.get("steps") or []:
            with_ = step.get("with") or {}
            if with_.get("repository") == "OmniNode-ai/omniclaude":
                with_["ref"] = "v9.9.9"
    violations = pin_move_violations(workflow, STRICT_GATE_JOBS)
    assert any("exactly one pin" in v for v in violations), violations


def test_pin_move_requires_integration_flags_a_missing_integration_step() -> None:
    workflow = copy.deepcopy(_load_workflow())
    steps = workflow["jobs"][DRIFT_JOB]["steps"]
    workflow["jobs"][DRIFT_JOB]["steps"] = [
        s for s in steps if s.get("name") != INTEGRATION_STEP
    ]
    violations = pin_move_violations(workflow, STRICT_GATE_JOBS)
    assert any(INTEGRATION_STEP in v for v in violations), violations


def test_pin_move_requires_integration_flags_a_gate_outside_ci_summary() -> None:
    relaxed = tuple(g for g in STRICT_GATE_JOBS if g != "Event Registry Drift")
    violations = pin_move_violations(_load_workflow(), relaxed)
    assert any("Event Registry Drift" in v for v in violations), violations


# =============================================================================
# AC3: one mutation per direction turns the production drift check red
# =============================================================================


def test_drift_strict_both_directions_control_is_green(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    omniclaude = make_omniclaude(tmp_path)
    root = make_repo_root(tmp_path, pin="v-pin")
    code, err = run_drift(root, omniclaude, capsys)
    assert code == 0, err


def test_drift_strict_both_directions_source_ahead_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """omniclaude registers an event the omnimarket registry lacks."""
    omniclaude = make_omniclaude(tmp_path, source_extra_at_pin=True)
    root = make_repo_root(tmp_path, pin="v-pin")
    code, err = run_drift(root, omniclaude, capsys)
    assert code == 1
    assert "Source topics missing from omnimarket registry" in err
    assert "Event types in omniclaude source but not in registry" in err


def test_drift_strict_both_directions_registry_ahead_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The omnimarket registry carries an event the pinned omniclaude lacks."""
    omniclaude = make_omniclaude(tmp_path)
    root = make_repo_root(tmp_path, pin="v-pin", registry_extra=True)
    code, err = run_drift(root, omniclaude, capsys)
    assert code == 1
    assert "Registry topics missing from omniclaude source" in err
    assert "Event types in registry but not in omniclaude source" in err


def test_drift_strict_both_directions_has_no_consumer_ahead_tolerance(
    tmp_path: Path,
) -> None:
    omniclaude = make_omniclaude(tmp_path)
    root = make_repo_root(tmp_path, pin="v-pin", registry_extra=True)
    with pytest.raises(SystemExit) as exit_info:
        drift_main(
            [
                "--repo-root",
                str(root),
                "--omniclaude-root",
                str(omniclaude),
                "--tolerate-consumer-ahead",
            ]
        )
    assert exit_info.value.code == 2


def test_drift_strict_both_directions_ci_job_passes_no_tolerance_flag() -> None:
    steps = _load_workflow()["jobs"][DRIFT_JOB]["steps"]
    drift_steps = [
        s
        for s in steps
        if "omnimarket.validators.event_registry_drift" in str(s.get("run", ""))
    ]
    assert drift_steps, "the CI job must run the production drift check"
    assert all("tolerate" not in str(s["run"]) for s in drift_steps)


# =============================================================================
# AC4: the two OMN-13146 tests are present and uninverted
# =============================================================================

OMN13146_TESTS = {
    "test_required_fields_drift_is_caught": True,
    "test_live_registries_have_no_unbaselined_drift": False,
}


def _has_drift_asserts(function: ast.FunctionDef) -> list[bool]:
    found: list[bool] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Attribute)
            and test.left.attr == "has_drift"
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Is)
            and isinstance(test.comparators[0], ast.Constant)
            and isinstance(test.comparators[0].value, bool)
        ):
            found.append(test.comparators[0].value)
    return found


def _decorator_names(function: ast.FunctionDef) -> list[str]:
    return [ast.unparse(decorator) for decorator in function.decorator_list]


def omn13146_violations(source: str) -> list[str]:
    tree = ast.parse(source)
    functions = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    violations: list[str] = []
    for name, expected in OMN13146_TESTS.items():
        function = functions.get(name)
        if function is None:
            violations.append(f"{name} is missing")
            continue
        decorators = _decorator_names(function)
        if "pytest.mark.unit" not in decorators:
            violations.append(f"{name} lost its pytest.mark.unit marker")
        skipping = [d for d in decorators if re.search(r"\b(skip|skipif|xfail)\b", d)]
        if skipping:
            violations.append(f"{name} is disabled by {skipping}")
        polarity = _has_drift_asserts(function)
        if polarity != [expected]:
            violations.append(
                f"{name} must assert report.has_drift is {expected}, found {polarity}"
            )
    required = functions.get("test_required_fields_drift_is_caught")
    if (
        required is not None
        and "in report.field_diffs['task.delegated']" not in ast.unparse(required)
    ):
        violations.append(
            "test_required_fields_drift_is_caught no longer inspects the "
            "task.delegated required_fields diff"
        )
    return violations


def test_omn13146_tests_guarded_live_file_holds() -> None:
    assert omn13146_violations(DRIFT_TEST_FILE.read_text()) == []


def test_omn13146_tests_guarded_flags_an_inverted_polarity() -> None:
    source = DRIFT_TEST_FILE.read_text()
    inverted = source.replace(
        "assert report.has_drift is True\n\n\n@pytest.mark.unit\ndef test_required_fields_order_is_not_drift",
        "assert report.has_drift is False\n\n\n@pytest.mark.unit\ndef test_required_fields_order_is_not_drift",
        1,
    )
    assert inverted != source, "positive control: the mutation must apply"
    violations = omn13146_violations(inverted)
    assert any(
        "test_required_fields_drift_is_caught must assert" in v for v in violations
    )


def test_omn13146_tests_guarded_flags_a_disabled_live_lock() -> None:
    source = DRIFT_TEST_FILE.read_text()
    disabled = source.replace(
        "@pytest.mark.unit\ndef test_live_registries_have_no_unbaselined_drift",
        "@pytest.mark.unit\n@pytest.mark.skip\ndef test_live_registries_have_no_unbaselined_drift",
        1,
    )
    assert disabled != source, "positive control: the mutation must apply"
    assert any("is disabled" in v for v in omn13146_violations(disabled))


def test_omn13146_tests_guarded_flags_a_deleted_test() -> None:
    source = DRIFT_TEST_FILE.read_text()
    renamed = source.replace(
        "def test_required_fields_drift_is_caught", "def test_renamed_away", 1
    )
    assert any("is missing" in v for v in omn13146_violations(renamed))
