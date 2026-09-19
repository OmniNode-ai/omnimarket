# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18790 — one test per acceptance-criterion falsifier for the skip-count ratchet.

Ported from omnibase_infra's OMN-18776 suite (squash 77ea8d01). Epic OMN-18775
measured five per-repo skip counts that were byte-identical across three
consecutive CI runs each; omnimarket's ``Tests (Split 1/20)`` was one of them, at
42/42/42. A count that never moves is a fixed set of tests that never runs, and
nothing on the fleet notices when that set grows — which is how 91 PostgreSQL-16
cases were collected and skipped on every run for 49 days.

Each test below is named for the falsifier it refuses. Deleting one deletes the
proof of that acceptance criterion.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any
from xml.sax.saxutils import quoteattr

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "skip_count_ratchet.py"
BASELINE = REPO_ROOT / "config" / "skip_count_baseline.yaml"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "skip_count_ratchet"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CI_SUMMARY_GATE = REPO_ROOT / "scripts" / "ci" / "ci_summary_gate.py"

SUITE = "omnimarket/test"
# Deliberately the OMN-18776 display name, not an OMN-18790 one: the fleet gets
# ONE name for one mechanism, so a cross-repo census can count carriers.
JOB_DISPLAY_NAME = "Skip Count Ratchet (OMN-18776)"

pytestmark = pytest.mark.unit


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )


def _junit(cases: list[tuple[str, bool]], collected: int) -> str:
    """Render a JUnit report. `cases` is (node_id, skipped) pairs.

    Attribute values are XML-escaped, which the omnibase_infra original did not
    need to do. omnimarket's parametrized ids carry shell fragments -- one
    baseline id embeds a whole ``grep -q '^status: PASS$' ...`` command -- so an
    unescaped renderer emits malformed XML and the gate fail-closes on its own
    fixture, which reads as a defect in the gate rather than in the test helper.
    """
    body = []
    for node_id, skipped in cases:
        classname, _, name = node_id.rpartition("::")
        inner = "<skipped message='synthetic'/>" if skipped else ""
        body.append(
            f"<testcase classname={quoteattr(classname)} "
            f"name={quoteattr(name)}>{inner}</testcase>"
        )
    joined = "".join(body)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<testsuites><testsuite name="pytest" errors="0" failures="0" '
        f'skipped="{sum(1 for _, s in cases if s)}" tests="{collected}" time="1.0">'
        f"{joined}</testsuite></testsuites>"
    )


def _baseline_entry() -> dict[str, Any]:
    loaded = yaml.safe_load(BASELINE.read_text(encoding="utf-8"))
    entry = loaded["suites"][SUITE]
    assert isinstance(entry, dict)
    return entry


def _baseline_ids() -> list[str]:
    ids = _baseline_entry()["node_ids"]
    assert isinstance(ids, list)
    return [str(i) for i in ids]


# ---------------------------------------------------------------- AC1


def test_ac1_shipped_baseline_records_provenance_for_every_entry() -> None:
    """Falsifier: a baseline entry with no recorded provenance."""
    loaded = yaml.safe_load(BASELINE.read_text(encoding="utf-8"))
    suites = loaded["suites"]
    assert suites, "positive control: the shipped baseline declares at least one suite"
    for key, entry in suites.items():
        provenance = entry.get("provenance")
        assert provenance, f"{key}: no provenance block"
        for field in ("measured_at", "measurement_command", "source_runs"):
            assert provenance.get(field), (
                f"{key}: provenance.{field} is missing or empty"
            )


def test_ac1_entry_missing_provenance_is_refused(tmp_path: Path) -> None:
    """Falsifier control: the validator must actually reject a bare entry."""
    bare = tmp_path / "baseline.yaml"
    bare.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "suites": {
                    SUITE: {
                        "repo": "omnimarket",
                        "job": "Tests",
                        "mode": "count",
                        "max_skips": 1,
                        "baseline_collected": 1,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    junit = tmp_path / "j.xml"
    junit.write_text(_junit([("a.b::test_c", True)], 1), encoding="utf-8")
    result = _run("--baseline", str(bare), "--suite", SUITE, "--junit", str(junit))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "provenance" in (result.stdout + result.stderr)


# ---------------------------------------------------------------- AC2


def test_ac2_added_environmental_skip_fails_naming_repo_job_and_delta(
    tmp_path: Path,
) -> None:
    """Falsifier: a deliberately added environmentally-skipped test produces a green run."""
    entry = _baseline_entry()
    cases: list[tuple[str, bool]] = [(i, True) for i in _baseline_ids()]
    cases.append(("tests.ci.test_synthetic_omn18790::test_needs_absent_service", True))
    junit = tmp_path / "j.xml"
    junit.write_text(_junit(cases, int(entry["baseline_collected"])), encoding="utf-8")

    result = _run("--baseline", str(BASELINE), "--suite", SUITE, "--junit", str(junit))
    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    assert "omnimarket" in out
    assert str(entry["job"]) in out
    assert "test_needs_absent_service" in out
    assert "+1" in out, "the failure must name the delta"


def test_ac2_positive_control_baseline_set_alone_passes(tmp_path: Path) -> None:
    """Positive control for the test above: without the added skip the run is green."""
    entry = _baseline_entry()
    cases = [(i, True) for i in _baseline_ids()]
    junit = tmp_path / "j.xml"
    junit.write_text(_junit(cases, int(entry["baseline_collected"])), encoding="utf-8")
    result = _run("--baseline", str(BASELINE), "--suite", SUITE, "--junit", str(junit))
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------- AC3


def test_ac3_removed_skip_passes_and_prints_a_ratchet_candidate(
    tmp_path: Path,
) -> None:
    """Falsifier: removing a skipped test turns the run red."""
    entry = _baseline_entry()
    ids = _baseline_ids()
    cases = [(i, True) for i in ids[:-1]]
    junit = tmp_path / "j.xml"
    junit.write_text(_junit(cases, int(entry["baseline_collected"])), encoding="utf-8")

    result = _run("--baseline", str(BASELINE), "--suite", SUITE, "--junit", str(junit))
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "RATCHET CANDIDATE" in out
    assert str(len(ids) - 1) in out, "the new lower number must be printed"


# ---------------------------------------------------------------- AC4


def test_ac4_ratchet_job_carries_no_continue_on_error() -> None:
    """Falsifier: `continue-on-error` appears anywhere in the new job."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert JOB_DISPLAY_NAME in text, "positive control: the job is declared in ci.yml"
    workflow = yaml.safe_load(text)
    job = workflow["jobs"]["skip-count-ratchet"]
    assert "continue-on-error" not in job
    for step in job["steps"]:
        assert "continue-on-error" not in step, step.get("name")

    # The falsifier is a grep, so the raw block must not even mention the
    # setting in prose -- a comment naming it reads as a hit to the same probe
    # that is supposed to prove its absence.
    block = text.split("\n  skip-count-ratchet:\n", 1)[1].split("\n  # ====", 1)[0]
    assert "skip_count_ratchet.py" in block, "positive control: the block was located"
    assert "continue-on-error" not in block


def test_ac4_ratchet_job_is_registered_under_the_ci_summary_umbrella() -> None:
    """Falsifier: the context appears in neither enforcement surface."""
    gate = CI_SUMMARY_GATE.read_text(encoding="utf-8")
    assert f'"{JOB_DISPLAY_NAME}"' in gate, (
        "omnimarket's merge gate is the `CI Summary` umbrella; an unregistered "
        "job that is skipped or absent yields SUCCESS"
    )
    strict_block = gate.split("STRICT_GATE_JOBS: tuple[str, ...] = (", 1)[1].split(
        "\n)", 1
    )[0]
    assert f'"{JOB_DISPLAY_NAME}"' in strict_block


def test_ac4_ratchet_job_is_unconditional() -> None:
    """A registered job that can legitimately skip wedges the umbrella; ours uses always()."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["skip-count-ratchet"]
    assert str(job.get("if", "")).strip() == "always()"


# ---------------------------------------------------------------- AC5
#
# omnimarket landed in `nodeids` mode, and the measurement that decided it is
# live rather than assumed. Three consecutive FULL-WIDTH runs (35395544621,
# 35393542842, 35392845454) each reported exactly 259 unique skipped ids with
# byte-identical SETS -- but all three were full-width only because each
# happened to touch test infrastructure. This ticket's own first CI run,
# 35414570676, was narrowed by `scripts/ci/detect_test_paths.py` to 765
# collected and 2 unique skips: the collected set here varies by a factor of 30,
# which is the `nodeids` shape, not the `count` shape. Under `count` that run
# printed a RATCHET CANDIDATE advising `max_skips` be lowered to 2; adopting it
# would have turned every subsequent full-width run red.


def test_ac5_selector_narrowed_real_run_does_not_false_fail() -> None:
    """Falsifier: a run fails on a count the impacted-test selector explains.

    The fixture is the REAL skipped-case set of run 35414570676 with its real
    collected total -- a genuinely selector-narrowed run of this repo, not a
    slice constructed to look like one.
    """
    result = _run(
        "--baseline",
        str(BASELINE),
        "--suite",
        SUITE,
        "--junit",
        str(FIXTURES / "narrowed-run-35414570676.xml"),
    )
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "narrowed selection" in out.lower()
    assert "RATCHET CANDIDATE" not in out, (
        "a narrowed run's lower count is the selector, not a ratchet opportunity"
    )


def test_ac5_positive_control_one_new_id_in_a_narrowed_run_still_fails(
    tmp_path: Path,
) -> None:
    """The zero above is not a broken probe: one unknown id in the same shape is red.

    This is the case `count` mode could not reach at all -- a narrowed run
    carrying a brand-new environmentally-skipped test stays far below the
    full-width number and passes a count comparison. Identity comparison
    refuses it.
    """
    original = (FIXTURES / "narrowed-run-35414570676.xml").read_text(encoding="utf-8")
    injected = original.replace(
        "</testsuite>",
        '<testcase classname="tests.ci.test_control_omn18790" '
        'name="test_positive_control"><skipped message="synthetic"/></testcase>'
        "</testsuite>",
    )
    assert injected != original, "positive control: the injection point was found"
    junit = tmp_path / "j.xml"
    junit.write_text(injected, encoding="utf-8")
    result = _run("--baseline", str(BASELINE), "--suite", SUITE, "--junit", str(junit))
    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    assert "test_positive_control" in out
    assert "+1" in out, "the failure must name the delta"


def test_ac5_mode_is_nodeids_and_the_two_halves_agree() -> None:
    """Falsifier: the entry is written back to `count`, or the halves drift apart.

    `count` is wrong for this suite and the reason is recorded in the baseline's
    own notes: the selector varies the collected set by a factor of 30, so a
    count comparison emits a ratchet-candidate number whose adoption reds every
    later full-width run. The loader enforces the count/identity agreement only
    in `nodeids` mode, which is a second reason the mode is load-bearing.
    """
    entry = _baseline_entry()
    assert entry["mode"] == "nodeids"
    ids = _baseline_ids()
    assert len(set(ids)) == len(ids), "the recorded node_ids carry a duplicate"
    assert int(entry["max_skips"]) == len(set(ids))


# ---------------------------------------------------------------- AC6


def test_ac6_parser_declares_no_override_option() -> None:
    """Falsifier: the argument parser declares any skip, force or baseline-override option.

    Lowering a baseline is an edit to the baseline file, reviewed like any other
    diff. A command-line lever would make it a decision one lane takes alone.
    """
    result = _run("--help")
    assert result.returncode == 0, result.stdout + result.stderr
    help_text = result.stdout.lower()
    forbidden = (
        "--force",
        "--skip",
        "--allow",
        "--ignore",
        "--override",
        "--no-fail",
        "--update-baseline",
        "--set-baseline",
        "--write-baseline",
        "--max-skips",
        "--tolerance",
        "--threshold",
        "--warn-only",
        "--advisory",
    )
    for option in forbidden:
        assert option not in help_text, f"{option} would make the ratchet optional"
    assert "--junit" in help_text, "positive control: the parser's help was read"


def test_ac6_no_environment_variable_lowers_the_verdict(tmp_path: Path) -> None:
    """An env var escape hatch is the same hole wearing a different hat."""
    entry = _baseline_entry()
    cases = [(i, True) for i in _baseline_ids()]
    cases.append(("tests.ci.test_synthetic_omn18790::test_env_escape", True))
    junit = tmp_path / "j.xml"
    junit.write_text(_junit(cases, int(entry["baseline_collected"])), encoding="utf-8")

    env_names = (
        "SKIP_COUNT_RATCHET",
        "SKIP_COUNT_RATCHET_FORCE",
        "SKIP_COUNT_RATCHET_ADVISORY",
        "ENABLE_SKIP_COUNT_RATCHET",
        "SKIP_RATCHET_OVERRIDE",
    )
    source = SCRIPT.read_text(encoding="utf-8")
    assert "os.environ" not in source, (
        "the gate must read no environment variable at all"
    )
    assert "getenv" not in source, "the gate must read no environment variable at all"
    for name in env_names:
        assert name not in source


# ---------------------------------------------------------------- fail-closed


def test_missing_junit_input_fails_closed(tmp_path: Path) -> None:
    result = _run(
        "--baseline",
        str(BASELINE),
        "--suite",
        SUITE,
        "--junit",
        str(tmp_path / "nope.xml"),
    )
    assert result.returncode == 2, result.stdout + result.stderr


def test_unparsable_junit_fails_closed(tmp_path: Path) -> None:
    junit = tmp_path / "j.xml"
    junit.write_text("this is not xml", encoding="utf-8")
    result = _run("--baseline", str(BASELINE), "--suite", SUITE, "--junit", str(junit))
    assert result.returncode == 2, result.stdout + result.stderr


def test_unknown_suite_fails_closed(tmp_path: Path) -> None:
    junit = tmp_path / "j.xml"
    junit.write_text(_junit([("a.b::test_c", True)], 1), encoding="utf-8")
    result = _run(
        "--baseline", str(BASELINE), "--suite", "nope/nope", "--junit", str(junit)
    )
    assert result.returncode == 2, result.stdout + result.stderr


def test_selftest_mode_passes_and_is_what_the_pre_commit_hook_runs() -> None:
    result = _run("--selftest")
    assert result.returncode == 0, result.stdout + result.stderr
    hook_config = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "skip_count_ratchet.py --selftest" in hook_config
