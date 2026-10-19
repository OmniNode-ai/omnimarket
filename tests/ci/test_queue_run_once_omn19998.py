# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run-once admission: queue tests are real; dev inherits only a proven tree."""

import ast
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.ci import ci_summary_gate as gate
from scripts.ci.detect_test_paths import compute_selection

ROOT = Path(__file__).resolve().parents[2]
ADJACENCY = ROOT / "scripts/ci/test_selection_adjacency.yaml"
CI = ROOT / ".github/workflows/ci.yml"


def _jobs():
    return [
        {"name": name, "status": "completed", "conclusion": "success"}
        for name in (*gate.STRICT_GATE_JOBS, *gate.SKIPPABLE_GATE_JOBS)
    ] + [
        {
            "name": f"Tests (Split {i}/20)",
            "status": "completed",
            "conclusion": "success",
        }
        for i in range(1, 21)
    ]


@pytest.mark.parametrize(
    "name",
    [
        "Detect Changes",
        "Coverage Sweep Gate",
        "Golden Chain Suite (inmemory bus)",
        "Tests (Split 3/20)",
        "Merge test durations",
    ],
)
def test_queue_refuses_skipped_heavy_job(name):
    jobs = _jobs()
    next(job for job in jobs if job["name"] == name)["conclusion"] = "skipped"
    code, report = gate.evaluate(jobs, event="merge_group")
    assert code == gate.EXIT_FAILURE, report


def test_queue_requires_all_twenty_shards():
    jobs = [job for job in _jobs() if job["name"] != "Tests (Split 20/20)"]
    code, report = gate.evaluate(jobs, event="merge_group")
    assert code == gate.EXIT_PENDING, report
    code, report = gate.evaluate(_jobs(), event="merge_group")
    assert code == gate.EXIT_SUCCESS, report


def test_queue_docs_marker_cannot_waive_full_suite():
    jobs = [
        *_jobs(),
        {
            "name": gate.DOCS_ONLY_MARKER_JOB,
            "status": "completed",
            "conclusion": "success",
        },
    ]
    next(job for job in jobs if job["name"] == "Coverage Sweep Gate")["conclusion"] = (
        "skipped"
    )
    code, report = gate.evaluate(jobs, event="merge_group")
    assert code == gate.EXIT_FAILURE, report


@pytest.mark.parametrize(
    "state", ["absent", "skipped", "failure", "cancelled", "success"]
)
def test_dev_skip_requires_tree_reuse_verdict(state):
    jobs = [
        job for job in _jobs() if not job["name"].startswith(gate.TEST_SPLIT_PREFIX)
    ]
    for job in jobs:
        if job["name"] in gate.QUEUE_HEAVY_GATE_JOBS:
            job["conclusion"] = "skipped"
    jobs = [job for job in jobs if job["name"] != gate.DEV_SUITE_REUSE_JOB]
    if state != "absent":
        jobs.append(
            {
                "name": gate.DEV_SUITE_REUSE_JOB,
                "status": "completed",
                "conclusion": state,
            }
        )
    code, report = gate.evaluate(jobs, event="push", ref_name="dev")
    expected = {"success": gate.EXIT_SUCCESS, "absent": gate.EXIT_PENDING}.get(
        state, gate.EXIT_FAILURE
    )
    assert code == expected, report


def test_dev_waits_for_reuse_and_never_relaxes_light_gate():
    jobs = _jobs()
    for job in jobs:
        if job["name"] in gate.QUEUE_HEAVY_GATE_JOBS:
            job["conclusion"] = "skipped"
        if job["name"] == gate.DEV_SUITE_REUSE_JOB:
            job.update(status="in_progress", conclusion=None)
    code, report = gate.evaluate(jobs, event="push", ref_name="dev")
    assert code == gate.EXIT_PENDING, report
    next(job for job in jobs if job["name"] == gate.DEV_SUITE_REUSE_JOB).update(
        status="completed", conclusion="success"
    )
    next(job for job in jobs if job["name"] == "lint")["conclusion"] = "skipped"
    code, report = gate.evaluate(jobs, event="push", ref_name="dev")
    assert code == gate.EXIT_FAILURE, report


def _dev_reuse_jobs(coverage_conclusion):
    jobs = [
        job for job in _jobs() if not job["name"].startswith(gate.TEST_SPLIT_PREFIX)
    ]
    for job in jobs:
        if job["name"] in gate.QUEUE_HEAVY_GATE_JOBS:
            job["conclusion"] = "skipped"
        if job["name"] == "Coverage Sweep Gate":
            job["conclusion"] = coverage_conclusion
    return jobs


@pytest.mark.parametrize(
    ("conclusion", "expected"),
    [
        ("skipped", gate.EXIT_SUCCESS),
        ("success", gate.EXIT_FAILURE),
        ("neutral", gate.EXIT_FAILURE),
        ("failure", gate.EXIT_FAILURE),
    ],
)
def test_dev_reuse_admits_only_a_skipped_heavy_gate(conclusion, expected):
    # The tree pass stands in for a heavy gate that did not run on the dev push;
    # a heavy gate that ran there is outside the reuse contract and stays strict.
    code, report = gate.evaluate(
        _dev_reuse_jobs(conclusion), event="push", ref_name="dev"
    )
    assert code == expected, report
    if expected == gate.EXIT_SUCCESS:
        assert "dev tree-reuse tier ACTIVE" in report, report


@pytest.mark.parametrize("ref_name", ["main", "hotfix/x", None])
def test_reuse_verdict_relaxes_nothing_off_dev(ref_name):
    code, report = gate.evaluate(
        _dev_reuse_jobs("skipped"), event="push", ref_name=ref_name
    )
    assert code == gate.EXIT_FAILURE, report
    assert "dev tree-reuse tier ACTIVE" not in report, report


def test_cli_passes_queue_event_to_admission(tmp_path):
    jobs = _jobs()
    next(job for job in jobs if job["name"] == "Detect Changes")["conclusion"] = (
        "skipped"
    )
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps(jobs))
    assert (
        gate.main(["--jobs-file", str(path), "--event", "merge_group"])
        == gate.EXIT_FAILURE
    )


def _condition(expression, values):
    """Interpret the workflow's boolean subset without executing source text."""
    for key, value in sorted(values.items(), key=lambda item: -len(item[0])):
        expression = expression.replace(key, repr(value))
    expression = (
        expression.replace("always()", "True")
        .replace("&&", " and ")
        .replace("||", " or ")
    )
    expression = re.sub(r"!(?!=)", " not ", expression)

    def visit(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not visit(node.operand)
        if isinstance(node, ast.BoolOp):
            values = [visit(value) for value in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = visit(node.left), visit(node.comparators[0])
            if isinstance(node.ops[0], ast.Eq):
                return left == right
            assert isinstance(node.ops[0], ast.NotEq)
            return left != right
        raise AssertionError(ast.dump(node))

    return visit(ast.parse(expression.strip(), mode="eval").body)


@pytest.mark.parametrize(
    ("event", "ref", "docs", "expected"),
    [
        ("merge_group", "refs/heads/gh-readonly-queue/dev/pr-1", "true", True),
        ("merge_group", "refs/heads/gh-readonly-queue/dev/pr-1", "false", True),
        ("pull_request", "refs/pull/1/merge", "true", False),
        ("pull_request", "refs/pull/1/merge", "false", True),
        ("push", "refs/heads/dev", "false", False),
        ("push", "refs/heads/main", "false", True),
        ("push", "refs/heads/hotfix/example", "false", True),
    ],
)
def test_heavy_job_event_admission(event, ref, docs, expected):
    jobs = yaml.safe_load(CI.read_text())["jobs"]
    for key in (
        "detect-changes",
        "integration-guard",
        "e2e-workflow-runner",
        "golden-chains",
        "event-chain-generation",
        "sea-e2e-chains",
        "generated-node-golden-chain",
        "coverage-sweep-gate",
    ):
        assert (
            _condition(
                jobs[key]["if"],
                {
                    "github.event_name": event,
                    "github.ref": ref,
                    "github.base_ref": "dev",
                    "github.event.pull_request.draft": False,
                    "needs.repo-evidence-preflight.result": "success",
                    "needs.zone-filter.outputs.docs_only": docs,
                },
            )
            is expected
        ), key


def test_pr_code_change_is_narrowed():
    selection = compute_selection(
        ["src/omnimarket/nodes/node_example/handler.py"], ADJACENCY, "feature"
    )
    assert not selection.is_full_suite
    assert "tests/nodes/" in selection.selected_paths


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        "scripts/ci/detect_test_paths.py",
        ".pre-commit-config.yaml",
        ".pre-commit-hooks/check.sh",
        "scripts/validation/check.py",
        "uv.lock",
    ],
)
def test_ci_change_in_mixed_pr_forces_full_suite(path):
    selection = compute_selection(
        ["src/omnimarket/nodes/node_example/handler.py", path], ADJACENCY, "feature"
    )
    assert selection.is_full_suite


def test_queue_always_selects_full_suite():
    selection = compute_selection(
        ["README.md"], ADJACENCY, "gh-readonly-queue/dev/pr-1", "merge_group"
    )
    assert selection.is_full_suite
    assert selection.matrix == list(range(1, 21))


def test_workflow_admission_and_marker_wiring():
    jobs = yaml.safe_load(CI.read_text())["jobs"]
    assert jobs["detect-changes"]["steps"][-1]["env"]["FLAG"] == "on"
    assert "merge_group" in jobs["detect-changes"]["if"]
    for key in (
        "detect-changes",
        "integration-guard",
        "e2e-workflow-runner",
        "golden-chains",
        "event-chain-generation",
        "sea-e2e-chains",
        "generated-node-golden-chain",
        "coverage-sweep-gate",
    ):
        assert "github.ref == 'refs/heads/dev'" in jobs[key]["if"]
    assert "merge_group" not in jobs["dev-full-suite-reuse"]["if"]
    assert (
        "full-suite-reuse.yml@624e03f3d7797709879b6c410708be6cc1061261"
        in jobs["dev-full-suite-reuse"]["uses"]
    )
    marker = next(
        step
        for step in jobs["ci-summary"]["steps"]
        if step["name"] == "Record full-suite tree pass"
    )
    assert "merge_group" in marker["if"]
    assert (
        marker["with"]["name"] == "full-suite-pass-${{ steps.suite-tree.outputs.tree }}"
    )


@pytest.mark.parametrize("tree", ["b" * 40, "unreadable"])
def test_marker_records_queue_sha_tree_and_refuses_unreadable_tree(tmp_path, tree):
    jobs = yaml.safe_load(CI.read_text())["jobs"]
    script = next(
        step["run"]
        for step in jobs["ci-summary"]["steps"]
        if step["name"] == "Read full-suite tree"
    )
    sha = "a" * 40
    gh = tmp_path / "gh"
    gh.write_text(
        '#!/bin/bash\nset -eu\ntest "$2" = "repos/example/repo/git/commits/$GITHUB_SHA"\nprintf "%s\\n" "$FAKE_TREE"\n'
    )
    gh.chmod(0o755)
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "GITHUB_OUTPUT": str(output),
            "GITHUB_SHA": sha,
            "GITHUB_REPOSITORY": "example/repo",
            "GITHUB_RUN_ID": "123",
            "FAKE_TREE": tree,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    if tree == "unreadable":
        assert result.returncode != 0
        assert not (tmp_path / "full-suite-pass.json").exists()
    else:
        assert result.returncode == 0, result.stderr
        assert output.read_text().strip() == f"tree={tree}"
        assert json.loads((tmp_path / "full-suite-pass.json").read_text()) == {
            "tree": tree,
            "sha": sha,
            "run_id": "123",
            "event": "merge_group",
        }


def test_required_contexts_have_queue_producers():
    workflows = {
        path.name: yaml.safe_load(path.read_text()) for path in CI.parent.glob("*.yml")
    }
    manifest = yaml.safe_load((ROOT / ".github/required-checks.yaml").read_text())
    mirrors = workflows["merge-group-pr-scoped-contexts.yml"]["jobs"]
    mirrored_names = {job["name"] for job in mirrors.values()}
    assert not (mirrored_names & {"CI Summary", *gate.QUEUE_HEAVY_GATE_JOBS})
    for required in manifest["gates"]:
        if required["mode"] != "REQUIRED" or required.get("branch") == "main":
            continue
        name = required["name"]
        if name in mirrored_names:
            continue
        producer = workflows[required.get("workflow", required.get("caller_workflow"))]
        assert "merge_group" in producer.get("on", producer.get(True, {})), name
    assert "repo-evidence / dod-verify" in mirrored_names


@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "skipped"])
def test_required_js_gate_reports_and_refuses_route_failure(result):
    job = yaml.safe_load(CI.read_text())["jobs"]["js-toolchain-hermetic"]
    assert job["if"] == "always()"
    assert "needs.route.outputs.runs_on" in job["runs-on"]
    assert "|| '[\"ubuntu-latest\"]'" in job["runs-on"]
    step = job["steps"][0]
    assert step["env"]["ROUTE_RESULT"] == "${{ needs.route.result }}"
    completed = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", step["run"]],
        env={**os.environ, "ROUTE_RESULT": result},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == (0 if result == "success" else 1)


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("squash-same-tree", 0),
        ("core-reused", 0),
        ("pr-producer", 1),
        ("other-workflow", 1),
        ("different-tree", 1),
        ("red-producer", 1),
        ("unreadable-producer", 1),
        ("no-marker", 1),
        ("queue-off", 0),
        ("queue-unreadable", 1),
    ],
)
def test_dev_reuses_queue_tree_across_squash_commit_identity(
    tmp_path, scenario, expected
):
    jobs = yaml.safe_load(CI.read_text())["jobs"]
    script = jobs["dev-suite-reuse-verdict"]["steps"][0]["run"]
    tree, pushed, queued = "a" * 40, "b" * 40, "c" * 40
    repo = "repos/example/repo"
    table = {
        f"{repo}/git/commits/{pushed}": tree,
        f"{repo}/actions/artifacts?name=full-suite-pass-{tree}&per_page=100": f"123 {queued}",
        f"{repo}/actions/runs/123": f"merge_group .github/workflows/ci.yml {queued}",
        f"{repo}/git/commits/{queued}": tree,
        f"{repo}/actions/runs/123/jobs?filter=latest&per_page=100": "completed success",
        f"{repo}/rules/branches/dev?per_page=100": "1",
    }
    if scenario == "pr-producer":
        table[f"{repo}/actions/runs/123"] = (
            f"pull_request .github/workflows/ci.yml {queued}"
        )
    elif scenario == "other-workflow":
        table[f"{repo}/actions/runs/123"] = (
            f"merge_group .github/workflows/other.yml {queued}"
        )
    elif scenario == "different-tree":
        table[f"{repo}/git/commits/{queued}"] = "d" * 40
    elif scenario == "red-producer":
        table[f"{repo}/actions/runs/123/jobs?filter=latest&per_page=100"] = (
            "completed failure"
        )
    elif scenario == "unreadable-producer":
        del table[f"{repo}/actions/runs/123"]
    elif scenario in {"no-marker", "queue-off", "queue-unreadable"}:
        table[f"{repo}/actions/artifacts?name=full-suite-pass-{tree}&per_page=100"] = ""
        if scenario == "queue-off":
            table[f"{repo}/rules/branches/dev?per_page=100"] = "0"
        elif scenario == "queue-unreadable":
            del table[f"{repo}/rules/branches/dev?per_page=100"]
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
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "FAKE_API": str(fixture),
            "GITHUB_REPOSITORY": "example/repo",
            "GITHUB_SHA": pushed,
            "REUSE": "true" if scenario == "core-reused" else "false",
            "PRODUCER_RUN": "123" if scenario == "core-reused" else "",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    if scenario == "queue-off":
        assert "::warning::No merge_queue rule is active on dev" in result.stdout


def test_dev_verdict_is_strict_while_a_queue_rule_is_active():
    jobs = yaml.safe_load(CI.read_text())["jobs"]
    script = jobs["dev-suite-reuse-verdict"]["steps"][0]["run"]
    assert 'select(.type == "merge_queue")' in script
    assert script.index('done <<< "$markers"') < script.index(
        "exit 0", script.index('if [ "$queue_rules" = "0" ]; then')
    )


def test_queue_provenance_jq_filters_match_api_shapes():
    jobs = yaml.safe_load(CI.read_text())["jobs"]
    script = jobs["dev-suite-reuse-verdict"]["steps"][0]["run"]
    filters = re.findall(r"--jq '([^']+)'", script)
    head = "a" * 40
    fixtures = [
        (
            {
                "artifacts": [
                    {"expired": False, "workflow_run": {"id": 123, "head_sha": head}},
                    {"expired": True, "workflow_run": {"id": 456, "head_sha": head}},
                ]
            },
            f"123 {head}",
        ),
        (
            {
                "event": "merge_group",
                "path": ".github/workflows/ci.yml@refs/heads/dev",
                "head_sha": head,
            },
            f"merge_group .github/workflows/ci.yml {head}",
        ),
        (
            {
                "jobs": [
                    {
                        "name": "CI Summary",
                        "status": "completed",
                        "conclusion": "success",
                    },
                    {"name": "other", "status": "completed", "conclusion": "failure"},
                ]
            },
            "completed success",
        ),
        (
            [{"type": "merge_queue", "ruleset_id": 14745045}, {"type": "pull_request"}],
            "1",
        ),
    ]
    assert len(filters) == len(fixtures)
    for expression, (payload, expected) in zip(filters, fixtures, strict=True):
        result = subprocess.run(
            ["jq", "-r", expression],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=True,
        )
        assert result.stdout.strip() == expected
    result = subprocess.run(
        ["jq", "-r", filters[-1]],
        input=json.dumps([{"type": "pull_request"}]),
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == "0"
