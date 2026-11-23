# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26446: a push to dev with no queue run recording the tree runs the Tests shards.

Before, the job ``Dev Push Full Suite Reuse`` looked for a queue run that recorded the
pushed tree, found none while no merge_queue rule was active on dev, printed a warning and
exited 0. The CI verdict on dev was then success with every Tests shard skipped, so two
pull requests that each passed on a narrowed selection (omnimarket#6567 and omnimarket#6580)
merged into a green dev together and failed six tests there.

Now the probe job records whether the tree is proven, ``detect-changes`` and ``test`` run
the full-suite shards when it is not, and the verdict job passes only on a proven tree or on
those shards passing. Every check here also runs against the pre-image of the two jobs
(tests/ci/fixtures/omn26446/pre_image_dev_push_jobs.yml, read from origin/dev before the
change) and must fail there: that is the RED control.

Select with ``-k dev_push_runs_test_shards`` (AC1) or ``-k dev_push_replay_6567_6580`` (AC2).
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.ci import ci_summary_gate as gate
from scripts.ci import merge_test_durations as durations
from scripts.ci.detect_test_paths import compute_selection

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github/workflows/ci.yml"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "omn26446"
PRE_IMAGE = FIXTURES / "pre_image_dev_push_jobs.yml"
REPLAY = FIXTURES / "dev_push_e3f5ee61c4.json"
ADJACENCY = ROOT / "scripts/ci/test_selection_adjacency.yaml"
PROBE = "dev-tree-pass-probe"
VERDICT = "dev-suite-reuse-verdict"
PROBE_RUN_TESTS = f"needs.{PROBE}.outputs.run_tests"
FULL_SPLITS = 20
TREE, PUSHED = "a" * 40, "b" * 40
REPO = "repos/example/repo"


def _live_jobs() -> dict[str, Any]:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]


def _pre_image_jobs() -> dict[str, Any]:
    return yaml.safe_load(PRE_IMAGE.read_text(encoding="utf-8"))


def _condition(expression: str, values: dict[str, str]) -> bool:
    """Interpret the workflow's boolean subset without executing source text."""
    for key, value in sorted(values.items(), key=lambda item: -len(item[0])):
        expression = expression.replace(key, repr(value))
    expression = (
        expression.replace("always()", "True")
        .replace("&&", " and ")
        .replace("||", " or ")
    )
    expression = re.sub(r"!(?!=)", " not ", expression)

    def visit(node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not visit(node.operand)
        if isinstance(node, ast.BoolOp):
            parts = [visit(value) for value in node.values]
            return all(parts) if isinstance(node.op, ast.And) else any(parts)
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = visit(node.left), visit(node.comparators[0])
            if isinstance(node.ops[0], ast.Eq):
                return left == right
            assert isinstance(node.ops[0], ast.NotEq)
            return left != right
        raise AssertionError(ast.dump(node))

    return bool(visit(ast.parse(expression.strip(), mode="eval").body))


def _detect_admitted(
    jobs: dict[str, Any], *, event: str, ref: str, **values: str
) -> bool:
    return _condition(
        jobs["detect-changes"]["if"],
        {
            "github.event_name": event,
            "github.ref": ref,
            "needs.repo-evidence-preflight.result": "success",
            "needs.zone-filter.outputs.docs_only": values.pop("docs_only", "false"),
            PROBE_RUN_TESTS: values.pop("run_tests", ""),
        },
    )


def _fake_gh(tmp_path: Path, table: dict[str, str]) -> dict[str, str]:
    """A gh that answers only the endpoints in the table, as the existing reuse tests do."""
    fixture = tmp_path / "api.json"
    fixture.write_text(json.dumps(table))
    gh = tmp_path / "gh"
    gh.write_text(
        f"#!{os.sys.executable}\n"
        "import json, os, sys\n"
        "table = json.load(open(os.environ['FAKE_API']))\n"
        "key = next(a for a in sys.argv if a.startswith('repos/'))\n"
        "if key not in table:\n"
        "    print('unreadable fixture endpoint', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "print(table[key])\n"
    )
    gh.chmod(0o755)
    return {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "FAKE_API": str(fixture),
        "GITHUB_REPOSITORY": "example/repo",
        "GITHUB_SHA": PUSHED,
    }


def _no_queue_no_marker_api() -> dict[str, str]:
    """dev with no merge_queue rule and no queue run that recorded the tree."""
    return {
        f"{REPO}/git/commits/{PUSHED}": TREE,
        f"{REPO}/actions/artifacts?name=full-suite-pass-{TREE}&per_page=100": "",
        f"{REPO}/rules/branches/dev?per_page=100": "0",
    }


def _run_step(
    jobs: dict[str, Any], job: str, tmp_path: Path, table: dict[str, str], **env: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    output = tmp_path / "github_output"
    output.write_text("")
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", jobs[job]["steps"][0]["run"]],
        cwd=tmp_path,
        env={
            **os.environ,
            **_fake_gh(tmp_path, table),
            "GITHUB_OUTPUT": str(output),
            "REUSE": "false",
            "PRODUCER_RUN": "",
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    outputs = dict(
        line.split("=", 1) for line in output.read_text().splitlines() if "=" in line
    )
    return result, outputs


def _verdict_exit(
    jobs: dict[str, Any],
    tmp_path: Path,
    *,
    tests: str,
    probe: str = "success",
    tree_pass: str = "false",
    run_tests: str = "true",
    detect: str = "success",
    full: str = "true",
) -> int:
    """Exit code of the dev push verdict step with the no-queue, no-marker API and these needs."""
    result, _ = _run_step(
        jobs,
        VERDICT,
        tmp_path,
        _no_queue_no_marker_api(),
        PROBE=probe,
        TREE_PASS=tree_pass,
        RUN_TESTS=run_tests,
        DETECT=detect,
        IS_FULL_SUITE=full,
        SPLIT_COUNT=str(FULL_SPLITS),
        TESTS=tests,
    )
    return result.returncode


# --------------------------------------------------------------------------- AC1


@pytest.mark.parametrize(
    ("event", "ref", "docs_only", "run_tests", "expected"),
    [
        # A dev push with no recorded tree pass runs the shards, even for a docs-only diff.
        ("push", "refs/heads/dev", "false", "true", True),
        ("push", "refs/heads/dev", "true", "true", True),
        # A proven tree, an unproven queue-active tree and an unreadable probe run none.
        ("push", "refs/heads/dev", "false", "false", False),
        ("push", "refs/heads/dev", "false", "", False),
        # Every other event is unchanged.
        ("merge_group", "refs/heads/gh-readonly-queue/dev/pr-1", "true", "", True),
        ("pull_request", "refs/pull/1/merge", "false", "", True),
        ("pull_request", "refs/pull/1/merge", "true", "", False),
        ("push", "refs/heads/main", "false", "", True),
    ],
)
def test_dev_push_runs_test_shards_admission(
    event: str, ref: str, docs_only: str, run_tests: str, expected: bool
) -> None:
    admitted = _detect_admitted(
        _live_jobs(), event=event, ref=ref, docs_only=docs_only, run_tests=run_tests
    )
    assert admitted is expected


def test_dev_push_runs_test_shards_chain_reaches_the_verdict() -> None:
    jobs = _live_jobs()
    assert PROBE in jobs["detect-changes"]["needs"]
    assert "always()" in jobs["detect-changes"]["if"], (
        "a PR run skips the dev-only probe; without always() detect-changes would skip with it"
    )
    assert "detect-changes" in jobs["test"]["needs"]
    assert {PROBE, "detect-changes", "test"} <= set(jobs[VERDICT]["needs"])
    assert jobs[VERDICT]["name"] == gate.DEV_SUITE_REUSE_JOB


def test_dev_push_runs_test_shards_selection_is_the_full_suite() -> None:
    steps = {step.get("name"): step for step in _live_jobs()["detect-changes"]["steps"]}
    for name in ("Resolve test selection", "Build selection record"):
        flag = steps[name]["env"]["FLAG"]
        assert "github.ref == 'refs/heads/dev'" in flag, name
        assert "github.event_name == 'push'" in flag, name
    # With the flag off the selector returns the policy's whole full-suite matrix.
    selection = compute_selection(
        ["README.md"], ADJACENCY, "dev", "push", feature_flag_enabled=False
    )
    assert selection.is_full_suite
    assert selection.matrix == list(range(1, FULL_SPLITS + 1))


def test_dev_push_runs_test_shards_probe_does_not_decide_on_the_warning(
    tmp_path: Path,
) -> None:
    result, outputs = _run_step(
        _live_jobs(), PROBE, tmp_path, _no_queue_no_marker_api()
    )
    assert result.returncode == 0, result.stderr
    assert "::warning::No merge_queue rule is active on dev" in result.stdout
    assert outputs == {"tree_pass": "false", "run_tests": "true"}


@pytest.mark.parametrize("queue_rules", ["1", "2"])
def test_dev_push_runs_test_shards_probe_stays_strict_with_a_queue_rule(
    tmp_path: Path, queue_rules: str
) -> None:
    table = _no_queue_no_marker_api()
    table[f"{REPO}/rules/branches/dev?per_page=100"] = queue_rules
    result, outputs = _run_step(_live_jobs(), PROBE, tmp_path, table)
    assert result.returncode == 1, result.stdout
    assert "run_tests" not in outputs


def test_dev_push_runs_test_shards_probe_inherits_a_proven_tree(tmp_path: Path) -> None:
    queued = "c" * 40
    table = {
        **_no_queue_no_marker_api(),
        f"{REPO}/actions/artifacts?name=full-suite-pass-{TREE}&per_page=100": f"123 {queued}",
        f"{REPO}/actions/runs/123": f"merge_group .github/workflows/ci.yml {queued}",
        f"{REPO}/git/commits/{queued}": TREE,
        f"{REPO}/actions/runs/123/jobs?filter=latest&per_page=100": "completed success",
    }
    result, outputs = _run_step(_live_jobs(), PROBE, tmp_path, table)
    assert result.returncode == 0, result.stderr
    assert outputs == {"tree_pass": "true", "run_tests": "false"}


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        # The shards of this push decide it.
        ({"tests": "success"}, 0),
        ({"tests": "failure"}, 1),
        ({"tests": "cancelled"}, 1),
        ({"tests": "skipped"}, 1),
        ({"tests": "success", "detect": "skipped"}, 1),
        ({"tests": "success", "detect": "failure"}, 1),
        ({"tests": "success", "full": "false"}, 1),
        # A proven tree is inherited, and nothing else is.
        ({"tests": "skipped", "tree_pass": "true", "run_tests": "false"}, 0),
        ({"tests": "skipped", "run_tests": "false"}, 1),
        ({"tests": "success", "probe": "failure", "run_tests": ""}, 1),
        ({"tests": "success", "probe": "skipped", "run_tests": ""}, 1),
    ],
)
def test_dev_push_runs_test_shards_verdict_needs_the_shards(
    tmp_path: Path, kwargs: dict[str, str], expected: int
) -> None:
    assert _verdict_exit(_live_jobs(), tmp_path, **kwargs) == expected


def _dev_push_snapshot(
    *, shard_conclusions: dict[int, str], verdict: str, shards_ran: bool
) -> list[dict[str, object]]:
    """The jobs a dev push leaves for the CI Summary: heavy jobs skipped unless the suite ran."""
    ran = {gate.DETECT_CHANGES_JOB, "Merge test durations"} if shards_ran else set()
    jobs: list[dict[str, object]] = []
    for name in dict.fromkeys((*gate.STRICT_GATE_JOBS, *gate.SKIPPABLE_GATE_JOBS)):
        if name == gate.DEV_SUITE_REUSE_JOB:
            continue
        heavy = name in gate.QUEUE_HEAVY_GATE_JOBS
        conclusion = "skipped" if heavy and name not in ran else "success"
        jobs.append({"name": name, "status": "completed", "conclusion": conclusion})
    jobs.append(
        {"name": gate.DEV_SUITE_REUSE_JOB, "status": "completed", "conclusion": verdict}
    )
    if shards_ran:
        jobs.extend(
            {
                "name": f"Tests (Split {i}/{FULL_SPLITS})",
                "status": "completed",
                "conclusion": shard_conclusions.get(i, "success"),
            }
            for i in range(1, FULL_SPLITS + 1)
        )
    return jobs


def test_dev_push_runs_test_shards_summary_accepts_only_a_complete_green_suite() -> (
    None
):
    green = _dev_push_snapshot(shard_conclusions={}, verdict="success", shards_ran=True)
    code, report = gate.evaluate(green, event="push", ref_name="dev")
    assert code == gate.EXIT_SUCCESS, report
    # One shard missing: the whole full suite is required, so the summary waits.
    partial = [job for job in green if job["name"] != f"Tests (Split 20/{FULL_SPLITS})"]
    code, report = gate.evaluate(partial, event="push", ref_name="dev")
    assert code == gate.EXIT_PENDING, report
    # A failed shard is red whatever the verdict job says.
    red = _dev_push_snapshot(
        shard_conclusions={3: "failure"}, verdict="success", shards_ran=True
    )
    code, report = gate.evaluate(red, event="push", ref_name="dev")
    assert code == gate.EXIT_FAILURE, report
    # The reuse tier is unchanged: a proven tree with every heavy job skipped is green.
    reused = _dev_push_snapshot(
        shard_conclusions={}, verdict="success", shards_ran=False
    )
    code, report = gate.evaluate(reused, event="push", ref_name="dev")
    assert code == gate.EXIT_SUCCESS, report
    # A ran-here Detect Changes is never relaxed to skipped by the reuse tier.
    stale = _dev_push_snapshot(shard_conclusions={}, verdict="success", shards_ran=True)
    for job in stale:
        if job["name"] == gate.DETECT_CHANGES_JOB:
            job["conclusion"] = "failure"
    code, report = gate.evaluate(stale, event="push", ref_name="dev")
    assert code == gate.EXIT_FAILURE, report


def test_dev_push_runs_test_shards_pre_image_is_red(tmp_path: Path) -> None:
    """RED control: the same checks against the pre-image jobs read from origin/dev."""
    pre = _pre_image_jobs()
    # It never admitted a dev push to detect-changes, whatever the tree.
    assert not _detect_admitted(
        pre, event="push", ref="refs/heads/dev", run_tests="true"
    )
    # It has no probe, and a verdict that does not wait for the shards...
    assert PROBE not in pre
    needs = pre[VERDICT]["needs"]
    assert "test" not in ([needs] if isinstance(needs, str) else needs)
    # ...so the warning alone admitted a push whose shards failed or never ran.
    for tests in ("failure", "skipped"):
        assert _verdict_exit(pre, tmp_path, tests=tests) == 0, tests
    # The checks of this file would each have failed on it.
    live = _live_jobs()
    assert _verdict_exit(live, tmp_path, tests="failure") == 1
    assert _detect_admitted(live, event="push", ref="refs/heads/dev", run_tests="true")


# --------------------------------------------------------------------------- AC2


def _replay() -> dict[str, Any]:
    return json.loads(REPLAY.read_text(encoding="utf-8"))


def _failing_shard(failing_file: str) -> int:
    _, _, shards = durations._plan(ROOT, durations.DEFAULT_RECORD, FULL_SPLITS)
    holding = [i for i, shard in enumerate(shards, 1) if failing_file in shard]
    assert len(holding) == 1, f"{failing_file} must be planned into exactly one shard"
    return holding[0]


def test_dev_push_replay_6567_6580_fixture_is_the_recorded_six() -> None:
    fixture = _replay()
    assert fixture["dev_sha"].startswith("e3f5ee61c4")
    assert fixture["pair"] == ["omnimarket#6567", "omnimarket#6580"]
    tests = fixture["failing_tests"]
    assert len(tests) == len(set(tests)) == 6
    assert {test.split("::", 1)[0] for test in tests} == {fixture["failing_file"]}
    assert (ROOT / fixture["failing_file"]).is_file()


def test_dev_push_replay_6567_6580_reads_red(tmp_path: Path) -> None:
    """The pair's dev push, with no queue run recording the tree, no longer reads green."""
    fixture = _replay()
    shard = _failing_shard(fixture["failing_file"])
    # The shard that plans the failing file fails; the verdict job reads the matrix result.
    verdict = _verdict_exit(_live_jobs(), tmp_path, tests="failure")
    assert verdict == 1
    snapshot = _dev_push_snapshot(
        shard_conclusions={shard: "failure"},
        verdict="failure" if verdict else "success",
        shards_ran=True,
    )
    code, report = gate.evaluate(snapshot, event="push", ref_name="dev")
    assert code == gate.EXIT_FAILURE, report
    assert f"Tests (Split {shard}/{FULL_SPLITS})" in report, report


def test_dev_push_replay_6567_6580_pre_image_read_green(tmp_path: Path) -> None:
    """RED control: the pre-image jobs read the same push as success, as dev CI did (run 44978250489)."""
    fixture = _replay()
    assert (
        fixture["recorded_dev_ci_verdict"] == "success with every Tests shard skipped"
    )
    verdict = _verdict_exit(_pre_image_jobs(), tmp_path, tests="skipped")
    assert verdict == 0
    snapshot = _dev_push_snapshot(
        shard_conclusions={},
        verdict="success" if verdict == 0 else "failure",
        shards_ran=False,
    )
    code, report = gate.evaluate(snapshot, event="push", ref_name="dev")
    assert code == gate.EXIT_SUCCESS, report
