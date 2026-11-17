# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""``--refuse-skips`` turns a run in which a test did not execute into a failure (OMN-19987).

An evidence check that runs a database proof passes today when the database is
absent: every test skips and pytest exits 0. A check that asks for
``--refuse-skips`` cannot pass that way.

Failure modes enumerated before the option existed, each with a case below:

* every test skips (no database) and the run exits 0;
* one test skips among tests that pass and the run exits 0;
* a whole module skips at import beside a module that passes, and the run exits 0;
* an expected failure is counted as proof;
* the option hides a real failure, or changes a clean run;
* the option changes any run that did not ask for it;
* the refusal does not say which test did not run, or why.

Each case runs pytest in a child process on a file under ``tmp_path``, with an
empty configuration file, so nothing in this repository's own test setup
decides the outcome.
"""

from __future__ import annotations

import ast
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from tests.ci.verifier_shell import (
    EVIDENCE_COLLECTOR,
    VERIFIER_TAG,
    VERIFIER_TAG_CMD,
    VERIFIER_TAG_DISPATCH,
    VERIFIER_TAG_RUN,
    VERIFIER_TAG_STATUS,
    verifier_shell,
    verifier_shell_at_tag,
    verifier_shell_from_source,
)

_ROOT = Path(__file__).resolve().parents[1]

_ALL_SKIP = """
import pytest

def test_a():
    pytest.skip("INTEGRATION_POSTGRES_PASSWORD not set")

def test_b():
    pytest.skip("INTEGRATION_POSTGRES_PASSWORD not set")
"""

_ONE_SKIP = """
import pytest

def test_passes():
    assert True

def test_skips():
    pytest.skip("no reachable Postgres")
"""

_MODULE_SKIP = """
import pytest

pytest.skip("module needs a database", allow_module_level=True)

def test_never_collected():
    assert True
"""

_XFAIL = """
import pytest

def test_passes():
    assert True

@pytest.mark.xfail(reason="known gap")
def test_expected_failure():
    assert False
"""

_ALL_PASS = """
def test_a():
    assert True

def test_b():
    assert True
"""

_ONE_FAILS = """
def test_passes():
    assert True

def test_fails():
    assert False
"""


def _run(
    tmp_path: Path, source: str, *options: str
) -> subprocess.CompletedProcess[str]:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_case.py").write_text(source, encoding="utf-8")
    # A second module that always passes: a module skipped at import is then
    # the only thing wrong with the run, as a skipped proof is beside green tests.
    (tmp_path / "test_other_module.py").write_text(
        "def test_other_module_passes():\n    assert True\n", encoding="utf-8"
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(tmp_path / "pytest.ini"),
            "--rootdir",
            str(tmp_path),
            "-p",
            "tests.refuse_skips",
            "-p",
            "no:cacheprovider",
            "-q",
            *options,
            str(tmp_path),
        ],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


@pytest.mark.parametrize(
    ("source", "named"),
    [
        (_ALL_SKIP, "INTEGRATION_POSTGRES_PASSWORD not set"),
        (_ONE_SKIP, "no reachable Postgres"),
        (_MODULE_SKIP, "module needs a database"),
        (_XFAIL, "test_expected_failure"),
    ],
    ids=["every_test_skips", "one_test_skips", "module_skips", "expected_failure"],
)
def test_a_run_in_which_a_test_did_not_prove_anything_is_refused(
    tmp_path: Path, source: str, named: str
) -> None:
    refused = _run(tmp_path, source, "--refuse-skips")
    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert "refuse-skips" in refused.stdout
    assert named in refused.stdout


@pytest.mark.parametrize(
    "source",
    [_ALL_SKIP, _ONE_SKIP, _MODULE_SKIP, _XFAIL],
    ids=["every_test_skips", "one_test_skips", "module_skips", "expected_failure"],
)
def test_the_same_run_without_the_option_is_unchanged(
    tmp_path: Path, source: str
) -> None:
    # The control: this is the exit 0 the option exists to refuse.
    plain = _run(tmp_path, source)
    assert plain.returncode == 0, plain.stdout + plain.stderr
    assert "refuse-skips" not in plain.stdout


def test_a_run_in_which_every_test_passes_still_passes(tmp_path: Path) -> None:
    clean = _run(tmp_path, _ALL_PASS, "--refuse-skips")
    assert clean.returncode == 0, clean.stdout + clean.stderr
    assert "refuse-skips" not in clean.stdout


def test_a_real_failure_stays_a_failure(tmp_path: Path) -> None:
    failed = _run(tmp_path, _ONE_FAILS, "--refuse-skips")
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert "test_fails" in failed.stdout


def test_a_run_that_selects_no_test_does_not_pass(tmp_path: Path) -> None:
    empty = _run(tmp_path, _ALL_PASS, "--refuse-skips", "-k", "no_such_test")
    assert empty.returncode != 0, empty.stdout + empty.stderr


def test_the_shared_conftest_offers_the_option(request: pytest.FixtureRequest) -> None:
    # The bound check runs plain ``uv run pytest ... --refuse-skips`` with no
    # ``-p``: the option has to come from this repository's own test setup.
    # This session is such a run, so its own configuration must know the option.
    from tests import conftest, refuse_skips

    assert request.config.getoption("refuse_skips") is False
    assert conftest.pytest_addoption is refuse_skips.pytest_addoption
    assert conftest.pytest_sessionfinish is refuse_skips.pytest_sessionfinish
    assert conftest.pytest_terminal_summary is refuse_skips.pytest_terminal_summary


# The AC2 binding of contracts/OMN-21217.yaml is one test with two
# parametrizations. Its check is the case this option exists for: before it asked
# for --refuse-skips, a run in which both parametrizations skipped exited 0.
_AC2_CONTRACT = _ROOT / "contracts" / "OMN-21217.yaml"
_AC2_ITEM = "dod-omn21217-ac2-accepted-20261117t0145z"
_SKIP_EVERY_TEST = """
import pytest

def pytest_collection_modifyitems(items):
    for item in items:
        item.add_marker(pytest.mark.skip(reason="every test skipped by the control"))
"""


def _ac2_check_value() -> str:
    contract = yaml.safe_load(_AC2_CONTRACT.read_text(encoding="utf-8"))
    items = [i for i in contract["dod_evidence"] if i["id"] == _AC2_ITEM]
    assert len(items) == 1, "the AC2 binding item is missing or repeated"
    assert items[0]["binds_ac"] == ["AC2"]
    checks = items[0]["checks"]
    assert len(checks) == 1
    value = checks[0]["check_value"]
    assert isinstance(value, str)
    return value


# The verifier runs a contract check under the shell its own runner names; the
# argv prefix is read from that runner's source (tests/ci/verifier_shell.py), not
# written here, so these tests stop passing when the verifier drops pipefail.
# Without pipefail a pipeline's status is the last stage's, which is the case the
# regression tests below pin.
_VERIFIER_SHELL = verifier_shell()
_NO_PIPEFAIL_SHELL = ["bash", "-c"]


def _run_command(
    tmp_path: Path,
    command: str,
    *,
    shell: list[str] = _VERIFIER_SHELL,
    cwd: Path = _ROOT,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(tmp_path), env.get("PYTHONPATH", "")) if part
    )
    return subprocess.run(
        [*shell, command],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
        env=env,
    )


def _run_check(
    tmp_path: Path, check_value: str, *extra: str
) -> subprocess.CompletedProcess[str]:
    """Run a contract check as the verifier does (bash, pipefail), with this interpreter."""
    assert check_value.startswith("set -o pipefail; uv run pytest ")
    (tmp_path / "skip_every_test.py").write_text(_SKIP_EVERY_TEST, encoding="utf-8")
    command = check_value.replace(
        "uv run pytest",
        f"{shlex.quote(sys.executable)} -m pytest -p no:cacheprovider {' '.join(extra)}",
        1,
    )
    return _run_command(tmp_path, command)


def _pytest_stage_and_filter(check_value: str) -> tuple[str, str]:
    """The pytest stage and the grep stage of the AC2 check, split at its one pipe."""
    stages = check_value.split(" | ")
    assert len(stages) == 2, check_value
    assert stages[1].startswith("grep -E "), stages[1]
    return stages[0], stages[1]


def test_the_shell_these_cases_run_under_is_the_verifiers_and_has_pipefail() -> None:
    # Read from the verifier's runner, so these cases follow it: a verifier that
    # dropped pipefail would change this prefix and fail the case below that
    # needs a failed pytest stage to fail the check.
    assert _VERIFIER_SHELL[0] == "bash"
    assert "pipefail" in _VERIFIER_SHELL
    assert _VERIFIER_SHELL[-1] == "-c"


def test_the_ac2_binding_check_asks_for_the_refusal_and_the_count() -> None:
    value = _ac2_check_value()
    assert "--refuse-skips" in value
    assert "2 passed" in value


def test_the_ac2_binding_check_passes_when_both_parametrizations_run(
    tmp_path: Path,
) -> None:
    # The positive control: the check is not one that always fails.
    done = _run_check(tmp_path, _ac2_check_value())
    assert done.returncode == 0, done.stdout + done.stderr
    assert "2 passed" in done.stdout


# The AC2 binding check is run by the local Done gate too, whose per-check budget
# is 30 s unless the operator sets DOD_VERIFY_CHECK_TIMEOUT_S. Each case below
# starts one to three nested pytest runs of about 8 s each, so the cases are small
# enough for the contract to select them in groups that each fit that budget.


def test_the_ac2_binding_check_old_form_exits_zero_when_both_skip(
    tmp_path: Path,
) -> None:
    value = _ac2_check_value()
    # The old check, as it stood before the option: no refusal, no count.
    old = value.split(" --refuse-skips")[0]
    assert "--refuse-skips" not in old
    control = _run_check(tmp_path, old, "-p", "skip_every_test")
    assert control.returncode == 0, control.stdout + control.stderr
    assert "2 skipped" in control.stdout


def test_the_ac2_binding_check_refusal_stage_names_both_skips(
    tmp_path: Path,
) -> None:
    # The pytest stage alone names the refusal and both parametrizations.
    pytest_stage, _ = _pytest_stage_and_filter(_ac2_check_value())
    refusal = _run_check(tmp_path, pytest_stage, "-p", "skip_every_test")
    assert refusal.returncode == 1, refusal.stdout + refusal.stderr
    assert "refuse-skips: tests that did not run" in refusal.stdout
    assert "[False]" in refusal.stdout
    assert "[True]" in refusal.stdout


def test_the_ac2_binding_check_whole_check_prints_no_count_when_both_skip(
    tmp_path: Path,
) -> None:
    # The whole check prints no "2 passed" line, so grep's status refuses it too.
    new = _run_check(tmp_path, _ac2_check_value(), "-p", "skip_every_test")
    assert new.returncode == 1, new.stdout + new.stderr
    assert "2 passed" not in new.stdout


def test_the_ac2_binding_check_old_form_exits_zero_when_only_one_runs(
    tmp_path: Path,
) -> None:
    # One parametrization selected: pytest exits 0 with one test run and nothing
    # skipped, so only the count in the check can refuse it.
    only_one = (
        _ac2_check_value().split(" --refuse-skips")[0] + " --refuse-skips -k False"
    )
    control = _run_check(tmp_path, only_one)
    assert control.returncode == 0, control.stdout + control.stderr
    assert "1 passed" in control.stdout
    assert "2 passed" not in control.stdout


def test_the_ac2_binding_check_fails_when_only_one_parametrization_runs(
    tmp_path: Path,
) -> None:
    value = _ac2_check_value()
    selected = value.replace(" --refuse-skips |", " --refuse-skips -k False |", 1)
    assert selected != value
    done = _run_check(tmp_path, selected)
    # grep finds no "2 passed" line: its status 1 is the refusal, and nothing is
    # printed because the only stage that prints is the one that matched nothing.
    assert done.returncode == 1, done.stdout + done.stderr
    assert done.stdout == ""


_PYTEST_FAILS_AFTER_TWO_PASS = """
import pytest

@pytest.fixture
def broken():
    raise RuntimeError("fixture refused")

def test_one():
    assert True

def test_two():
    assert True

def test_errors(broken):
    assert True
"""


def test_the_ac2_binding_filter_alone_passes_a_failed_run_that_prints_the_counted_line(
    tmp_path: Path,
) -> None:
    # The hazard the verifier's pipefail closes. pytest ends "2 passed, 1 error"
    # and exits 1; the grep stage matches that line. Without pipefail the
    # pipeline's status is grep's (0), so the check would pass a run in which
    # pytest failed. This is the control: it must read 0.
    _, filter_stage = _pytest_stage_and_filter(_ac2_check_value())
    (tmp_path / "test_mixed.py").write_text(_PYTEST_FAILS_AFTER_TWO_PASS, "utf-8")
    command = (
        f"{shlex.quote(sys.executable)} -m pytest -p no:cacheprovider -q "
        f"{shlex.quote(str(tmp_path / 'test_mixed.py'))} | {filter_stage}"
    )
    plain = _run_command(tmp_path, command, shell=_NO_PIPEFAIL_SHELL, cwd=tmp_path)
    assert plain.returncode == 0, plain.stdout + plain.stderr
    assert "2 passed, 1 error" in plain.stdout


def test_the_ac2_binding_check_fails_when_pytest_fails_but_the_counted_line_prints(
    tmp_path: Path,
) -> None:
    # The same run under the verifier's shell: the matching line is printed,
    # and the check still fails because pytest's status (1) is the pipeline's.
    _, filter_stage = _pytest_stage_and_filter(_ac2_check_value())
    (tmp_path / "test_mixed.py").write_text(_PYTEST_FAILS_AFTER_TWO_PASS, "utf-8")
    command = (
        f"{shlex.quote(sys.executable)} -m pytest -p no:cacheprovider -q "
        f"{shlex.quote(str(tmp_path / 'test_mixed.py'))} | {filter_stage}"
    )
    done = _run_command(tmp_path, command, cwd=tmp_path)
    assert "2 passed, 1 error" in done.stdout, done.stdout + done.stderr
    assert done.returncode == 1, done.stdout + done.stderr


def test_the_ac2_binding_check_is_not_rescued_by_a_stubbed_pytest_line(
    tmp_path: Path,
) -> None:
    # A stage that prints the counted line and exits non-zero, in place of the
    # pytest stage of the real check, is refused under the verifier's shell.
    _, filter_stage = _pytest_stage_and_filter(_ac2_check_value())
    command = f"(echo '2 passed, 1 error in 0.10s'; exit 3) | {filter_stage}"
    refused = _run_command(tmp_path, command)
    assert refused.returncode == 3, refused.stdout + refused.stderr
    assert "2 passed, 1 error" in refused.stdout
    unguarded = _run_command(tmp_path, command, shell=_NO_PIPEFAIL_SHELL)
    assert unguarded.returncode == 0, unguarded.stdout + unguarded.stderr


# --- the verifier the hosted gate installs (verifier-tag) -----------------------
#
# The cases above run under the shell read from this tree's collector. The hosted
# gate installs the verifier at the caller's ``verifier-tag``, a tag this tree's
# collector may have moved past, so the shell the AC2 check really runs under
# there is the one that tag's runner names.

_CALLER = _ROOT / ".github" / "workflows" / "call-repo-evidence-gate.yml"
_COLLECTOR_REL = EVIDENCE_COLLECTOR.relative_to(_ROOT).as_posix()


def _caller_verifier_tag() -> str:
    caller = yaml.safe_load(_CALLER.read_text(encoding="utf-8"))
    tag = caller["jobs"]["repo-evidence"]["with"]["verifier-tag"]
    assert isinstance(tag, str)
    return tag


def _collector_at_tag() -> list[str] | None:
    """The collector's lines at the pinned tag, or None where the clone lacks it."""
    shown = subprocess.run(
        ["git", "-C", str(_ROOT), "show", f"{VERIFIER_TAG}:{_COLLECTOR_REL}"],
        capture_output=True,
        text=True,
        check=False,
        env=scrub_git_location_env(os.environ),
    )
    return shown.stdout.splitlines() if shown.returncode == 0 else None


def test_the_verifier_tag_the_caller_installs_is_the_one_whose_runner_is_recorded() -> (
    None
):
    # Moving the pin without re-reading the runner's lines fails here.
    assert _caller_verifier_tag() == VERIFIER_TAG


def test_the_runner_at_the_verifier_tag_has_pipefail_and_runs_a_test_passes_check() -> (
    None
):
    shell = verifier_shell_at_tag()
    assert shell == ["bash", "-o", "pipefail", "-c"]
    # a test_passes check takes the one command path, with the check's own text
    assert 'check_type in ("command", "test_passes")' in VERIFIER_TAG_DISPATCH[0]
    assert "_run_command_check(" in VERIFIER_TAG_DISPATCH[1]
    assert "cmd_str = check.get(" in VERIFIER_TAG_CMD[0]
    assert VERIFIER_TAG_RUN[1].endswith("cmd_str],")
    # any non-zero status fails the check, so pytest's exit 5 fails it too
    assert VERIFIER_TAG_STATUS[0].strip() == "if result.returncode != 0:"


def _require_the_tag_object(
    required: bool, lines: list[str] | None
) -> list[str] | None:
    """The collector's lines, or None where the clone lacks the tag and it is not required."""
    if lines is None and required:
        raise AssertionError(
            f"the verifier tag {VERIFIER_TAG} is not in this clone, so the recorded "
            "runner lines were not compared with the verifier"
        )
    return lines


def test_the_recorded_runner_lines_are_the_lines_of_the_collector_at_the_tag() -> None:
    # Compared with the tag's own file wherever the clone holds that object. A
    # shallow hosted checkout does not, and there the pin test above is what
    # holds; the local Done gate item of contracts/OMN-25198.yaml sets
    # REQUIRE_VERIFIER_TAG, which turns the absence of the tag into a failure.
    lines = _require_the_tag_object(
        os.environ.get("REQUIRE_VERIFIER_TAG") == "1", _collector_at_tag()
    )
    if lines is None:
        return
    # Each recorded line is a line of the tag's file, in order, and the run call
    # is the contiguous block that file holds.
    for recorded in (VERIFIER_TAG_DISPATCH, VERIFIER_TAG_CMD, VERIFIER_TAG_STATUS):
        positions = [lines.index(line) for line in recorded]
        assert positions == sorted(positions), recorded
    start = lines.index(VERIFIER_TAG_RUN[0])
    assert tuple(lines[start : start + len(VERIFIER_TAG_RUN)]) == VERIFIER_TAG_RUN


def test_the_recorded_runner_comparison_fails_when_the_tag_is_required_and_absent() -> (
    None
):
    with pytest.raises(AssertionError, match="not in this clone"):
        _require_the_tag_object(True, None)
    assert _require_the_tag_object(False, None) is None
    assert _require_the_tag_object(True, ["x"]) == ["x"]


def test_the_recorded_runner_comparison_refuses_a_recorded_line_the_tag_lacks() -> None:
    # The control: the comparison is not one that always passes. A tag file
    # without the runner's call fails the lookup the real comparison does.
    lines = (_collector_at_tag() or list(VERIFIER_TAG_RUN)).copy()
    lines = [line for line in lines if "pipefail" not in line]
    with pytest.raises(ValueError, match="not in list"):
        lines.index(VERIFIER_TAG_RUN[1])


def test_a_local_done_gate_item_reads_the_verifier_tag_before_the_recorded_runner_tests() -> (
    None
):
    contract = yaml.safe_load(
        (_ROOT / "contracts" / "OMN-25198.yaml").read_text(encoding="utf-8")
    )
    found = [
        i
        for i in contract["dod_evidence"]
        if i["id"] == "dod-omn25198-verifier-tag-read"
    ]
    assert len(found) == 1
    item = found[0]
    assert item["execution_scope"] == "local_done_gate"
    assert "binds_ac" not in item
    (check,) = item["checks"]
    value = check["check_value"]
    assert value.startswith(f"git cat-file -e '{VERIFIER_TAG}^{{commit}}' && ")
    assert "REQUIRE_VERIFIER_TAG=1" in value
    assert "tests/test_refuse_skips_omn19987.py" in value
    assert "-k recorded_runner" in value
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    selected = [
        n.name
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and "recorded_runner" in n.name
    ]
    assert len(selected) >= 3, selected


def test_the_ac2_check_sets_pipefail_itself_so_a_runner_without_it_still_fails_it(
    tmp_path: Path,
) -> None:
    # The binding does not rest on the runner's shell, which a test here can only
    # read: the check names the option in its own text. Under a plain `bash -c`
    # a first stage that prints the counted line and exits 3 still fails it, and
    # the same check with the option removed passes there (the control).
    value = _ac2_check_value()
    assert value.startswith("set -o pipefail; ")
    _, filter_stage = _pytest_stage_and_filter(value)
    stub = "(echo '2 passed, 1 error in 0.10s'; exit 3)"
    with_option = f"set -o pipefail; {stub} | {filter_stage}"
    without = f"{stub} | {filter_stage}"
    assert _run_command(tmp_path, with_option, shell=_NO_PIPEFAIL_SHELL).returncode == 3
    assert _run_command(tmp_path, without, shell=_NO_PIPEFAIL_SHELL).returncode == 0


def test_the_runner_reader_sees_a_tag_runner_that_drops_pipefail() -> None:
    # The control: the reader of the recorded lines is not one that always says
    # pipefail. The same lines with the option removed read as a shell without it.
    import textwrap

    mutant = "\n".join(VERIFIER_TAG_RUN).replace('"-o", "pipefail", ', "")
    assert mutant != "\n".join(VERIFIER_TAG_RUN)
    assert "pipefail" not in verifier_shell_from_source(textwrap.dedent(mutant))


def test_the_stubbed_pytest_line_is_refused_under_the_shell_of_the_verifier_tag(
    tmp_path: Path,
) -> None:
    _, filter_stage = _pytest_stage_and_filter(_ac2_check_value())
    command = f"(echo '2 passed, 1 error in 0.10s'; exit 3) | {filter_stage}"
    refused = _run_command(tmp_path, command, shell=verifier_shell_at_tag())
    assert refused.returncode == 3, refused.stdout + refused.stderr
    assert _run_command(tmp_path, command, shell=_NO_PIPEFAIL_SHELL).returncode == 0
