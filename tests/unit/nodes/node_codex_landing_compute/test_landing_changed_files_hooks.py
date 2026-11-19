# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Codex landing decisions through the node's wire input and result."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import TypeAdapter

from omnimarket.nodes.node_codex_landing_compute import (
    ModelCodexLandingRequest,
    ModelCodexLandingResult,
    NodeCodexLandingCompute,
)

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).resolve().parents[3] / "fixtures" / "codex_landing_2026_11_18.json"
)
FILES = ("src/module.py", "tests/test module.py")
HEAD = "a" * 40
PR = "example/project#1"
CONTRACT = Path(__file__).resolve().parents[4] / "contracts" / "OMN-25689.yaml"
LINT_FAILURE = "Lint.....................................Failed\n- hook id: lint\n- exit code: 1\n\n"
NEW_VIOLATION = LINT_FAILURE + "module.py: new violation"


def landing(**facts):
    payload = {"worktree": "$W", **facts}
    request = TypeAdapter(ModelCodexLandingRequest).validate_json(json.dumps(payload))
    return TypeAdapter(ModelCodexLandingResult).dump_python(
        NodeCodexLandingCompute().handle(request)
    )


def hook_result(hook, returncode, output, **facts):
    return {"hook": hook, "returncode": returncode, "output": output, **facts}


def handoff(report, install_verdict, detail=""):
    return {
        "report": report,
        "install_verdict": install_verdict,
        "install_detail": detail,
        "head": HEAD,
        "pr": PR,
    }


def test_changed_files_only_returns_one_exact_argv_per_commit_hook():
    result = landing(hook_plan={"hooks": ["lint", "format"], "files": FILES})
    assert result.get("hook_argv") == (
        ("pre-commit", "run", "lint", "--files", *FILES),
        ("pre-commit", "run", "format", "--files", *FILES),
    )


def test_changed_files_only_empty_scope_cannot_fall_back_to_default_files():
    result = landing(hook_plan={"hooks": ["lint"], "files": []})
    assert result.get("hook_argv") == ()


def test_changed_files_only_preserves_push_stage_and_handles_option_like_paths():
    result = landing(
        hook_plan={"hooks": ["guard"], "files": ["--all-files"], "stage": "pre-push"}
    )
    assert result.get("hook_argv") == (
        (
            "pre-commit",
            "run",
            "guard",
            "--hook-stage",
            "pre-push",
            "--files",
            "./--all-files",
        ),
    )


def test_packaged_node_dispatch_accepts_nested_hook_inputs(tmp_path):
    payload = {
        "worktree": "$W",
        "hook_plan": {"hooks": ["lint"], "files": FILES},
        "handoff": handoff(
            "Lab: host=h051 lane=probe command=pytest observed=3 passed", "NO"
        ),
    }
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    env = os.environ.copy()
    env.pop("OMNIBASE_PATH", None)

    done = subprocess.run(
        [
            str(Path(sys.executable).with_name("onex")),
            "node",
            "node_codex_landing_compute",
            "--input",
            str(input_path),
            "--output",
            "receipt",
            "--state-root",
            str(tmp_path / "state"),
            "--timeout",
            "30",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )

    assert done.returncode == 0, done.stderr + done.stdout
    receipt = json.loads(done.stdout)
    assert receipt["status"] == "success"
    assert receipt["result"]["hook_argv"] == [
        ["pre-commit", "run", "lint", "--files", *FILES]
    ]
    assert receipt["result"]["handoff_decision"]["notice"]["head"] == HEAD


def test_base_inherited_failure_keeps_hook_id_and_first_output_line():
    lane_run = hook_result(
        "lint",
        1,
        LINT_FAILURE + "/tmp/lane/src/module.py: shared violation",
        worktree="/tmp/lane",
    )
    base_run = hook_result(
        "lint",
        1,
        LINT_FAILURE + "/tmp/base/src/module.py: shared violation",
        worktree="/tmp/base",
    )
    result = landing(
        hook_comparison={"files": FILES, "lane": [lane_run], "base": [base_run]}
    )

    decision = result.get("hook_decision")
    assert decision is not None
    assert decision["may_commit"] is True
    (failure,) = decision["failures"]
    assert failure["classification"] == "base-inherited"
    assert failure["hook"] == "lint"
    assert failure["first_line"] == LINT_FAILURE.splitlines()[0]
    assert failure["about_change"] is True


@pytest.mark.parametrize(
    "base",
    [
        [hook_result("lint", 0, "Lint passed")],
        [hook_result("lint", 1, LINT_FAILURE + "legacy.py: base violation")],
        [hook_result("other", 1, NEW_VIOLATION)],
        [hook_result("lint", 1, NEW_VIOLATION, stage="pre-push")],
        [hook_result("lint", None, NEW_VIOLATION)],
        [],
    ],
)
def test_base_inherited_requires_the_same_failure(base):
    result = landing(
        hook_comparison={
            "files": FILES,
            "lane": [hook_result("lint", 1, NEW_VIOLATION)],
            "base": base,
        },
    )

    decision = result.get("hook_decision")
    assert decision is not None
    assert decision["may_commit"] is False
    (failure,) = decision["failures"]
    assert failure["classification"] == "fixable"
    assert failure["hook_class"] == "CODE"


@pytest.mark.parametrize(
    ("run", "classification", "hook_class"),
    [
        (
            hook_result("lint", 0, "Lint mutated files", rewrote_files=True),
            "fixable",
            "CODE",
        ),
        (hook_result("lint", 1, "No hook with id `lint`"), "fixable", "CODE"),
        (hook_result("lint", None, "Lint timed out"), "blocked", "CODE"),
        (
            hook_result("lint", 1, "- exit code: 128\nfatal: not a git repository"),
            "blocked",
            "ENV",
        ),
        (hook_result("lint", 1, "- exit code: 2\nusage: lint"), "blocked", "CODE"),
    ],
)
def test_base_inherited_does_not_clear_incomplete_runs_or_rewrites(
    run, classification, hook_class
):
    result = landing(hook_comparison={"files": FILES, "lane": [run], "base": [run]})

    decision = result.get("hook_decision")
    assert decision is not None
    assert decision["may_commit"] is False
    (failure,) = decision["failures"]
    assert failure["classification"] == classification
    assert failure["hook_class"] == hook_class


@pytest.mark.parametrize(
    ("report", "verdict"),
    [
        ("Lab: host=h051 lane=probe command=pytest observed=4 passed", "NA"),
        (f"Lab: installed=h051:{HEAD[:12]} observed=the hook fired", "OK"),
    ],
)
def test_handoff_needs_proof_accepts_the_corresponding_lab_line(report, verdict):
    decision = landing(handoff=handoff(report, verdict)).get("handoff_decision")
    assert decision == {"handoff": True, "notice": None}


@pytest.mark.parametrize(
    ("report", "verdict", "missing"),
    [
        (
            "Lab: host=h051 lane=probe command=pytest observed=4 passed",
            "NO",
            "installed=",
        ),
        (f"Lab: installed=h051:{HEAD[:12]} observed=tests passed", "NO", "installed="),
        (
            f"Lab: installed=h051:{HEAD[:12]}\nLab: observed=the hook ran",
            "OK",
            "installed=",
        ),
        (f"I ran installed=h051:{HEAD[:12]} observed=the hook ran", "OK", "installed="),
        ("Lab: installed=unknown observed=the hook ran", "OK", "installed="),
        (
            f"Lab: installed=h051:{HEAD[:12]} observed=the hook ran",
            "REFUSED",
            "install",
        ),
        ("Lab: host=h051 lane=probe command=pytest observed=", "NA", "observed="),
    ],
)
def test_handoff_needs_proof_returns_one_parent_notice_with_pr_and_head(
    report, verdict, missing
):
    decision = landing(handoff=handoff(report, verdict)).get("handoff_decision")
    assert decision is not None
    assert decision["handoff"] is False
    assert decision["notice"]["pr"] == PR
    assert decision["notice"]["head"] == HEAD
    assert missing in decision["notice"]["missing_proof"]


def test_recorded_2026_11_18_uses_changed_paths_and_defers_handoff_without_proof():
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert cases["A"]["codex_landing"] == [
        ["COMMIT", "none: no staged change or no codex-commit.msg"]
    ]
    assert "timed out with exit 124" in cases["A"]["final_message"]
    assert cases["B"]["closeout"].endswith(":uncommitted-changes")

    # Supply two staged paths to replay against a manifest the recorded lane withheld.
    result = landing(
        hook_plan={"hooks": ["lint"], "files": FILES},
        hook_comparison={
            "files": FILES,
            "lane": [hook_result("lint", None, cases["A"]["final_message"])],
            "base": [],
        },
        handoff=handoff(cases["B"]["blocker"], "NO"),
    )
    assert result.get("hook_argv") == (
        ("pre-commit", "run", "lint", "--files", *FILES),
    )
    assert result["hook_decision"]["may_commit"] is False
    assert result["hook_decision"]["failures"][0]["classification"] == "blocked"
    assert result["handoff_decision"]["handoff"] is False
    assert result["handoff_decision"]["notice"]["pr"] == PR
    assert "installed=" in result["handoff_decision"]["notice"]["missing_proof"]


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({}, ""),
        ({"host": "other"}, "expected a Codex receipt from h051"),
        (
            {"landing_failure": {"shape": "push-hook-refused"}},
            "contains a landing failure",
        ),
        ({"landing-shape": "push-hook-refused"}, "contains a landing failure"),
        (
            {
                "codex_landing": [
                    ["COMMIT", "none: no staged change or no codex-commit.msg"]
                ]
            },
            "does not name a commit SHA",
        ),
        (
            {"codex_landing": [["COMMIT", f"ok sha={HEAD}"]]},
            "does not show the scratch branch push",
        ),
    ],
)
def test_probe_receipt_verifier_checks_the_real_receipt_fields(
    tmp_path, changes, error
):
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    command = next(
        item["checks"][0]["check_value"]
        for item in contract["dod_evidence"]
        if item["id"] == "dod-omn25689-ac6-live-probe"
    )
    receipt = {
        "host": "h051",
        "model": "codex",
        "codex_landing": [
            ["COMMIT", f"ok sha={HEAD}"],
            ["PUSH", "ok branch=omn-25689-probe-1"],
        ],
        **changes,
    }
    receipt_path = tmp_path / "probe.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    done = subprocess.run(
        ["bash", "-c", command],
        env={**os.environ, "ONEX_CODEX_LANDING_PROBE_RECEIPT": str(receipt_path)},
        capture_output=True,
        text=True,
        check=False,
    )

    if error:
        assert done.returncode == 1
        assert error in done.stderr
    else:
        assert done.returncode == 0, done.stderr
        assert "contains a commit SHA and scratch branch push" in done.stdout
