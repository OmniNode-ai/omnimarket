# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canary fixtures for the Phase 3 Product Readiness reason-graph (OMN-14703).

These are the omnimarket slice of the design fixture matrix
(``docs/plans/2026-07-17-product-first-ci-decouple-design.md`` §4). They prove
that a seeded product failure surfaces in Product Readiness as a typed
``PRODUCT_FAILED`` root, that the reason-graph is single-rooted and replay-
deterministic, and — structurally — that the shadow surface has NO ``occ-preflight``
in its needs-chain and mints NO OCC request.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ci.product_reason_graph import (
    DEPLOY_TRIGGER_FAILED,
    EVIDENCE_MISSING,
    GITHUB_API_OUTAGE,
    POLICY_HELD,
    PRODUCT_FAILED,
    RUNNER_INFRA,
    STATUS_BLOCKED_UPSTREAM,
    STATUS_FAILED,
    build_reason_graph,
    root_receipt_id,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "product_reason_graph.py"
_SHADOW_WF = _REPO_ROOT / ".github" / "workflows" / "product-readiness-shadow.yml"
_REUSABLE_WF = _REPO_ROOT / ".github" / "workflows" / "product-readiness.yml"

_HEAD = "a" * 40


def _green_subchecks() -> dict[str, str]:
    return dict.fromkeys(
        ("change_detection", "lint", "typecheck", "tests", "coverage"), "success"
    )


# --------------------------------------------------------------------------
# Seeded product failures — PRODUCT_FAILED root, OCC-independent.
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("failing_check", "expected_signal"),
    [
        ("lint", "lint=failure"),  # seeded-lint-fail-market
        ("typecheck", "typecheck=failure"),  # seeded-typecheck-fail-market
    ],
)
def test_seeded_product_failure_roots_as_product_failed(
    failing_check: str, expected_signal: str
) -> None:
    subchecks = _green_subchecks()
    subchecks[failing_check] = "failure"
    graph = build_reason_graph({"head_sha": _HEAD, "subchecks": subchecks})

    assert graph["root"] is not None
    assert graph["root"]["kind"] == PRODUCT_FAILED
    assert graph["root"]["primary_signal"] == expected_signal
    assert graph["blocked_candidate_count"] == 1
    # The failing subcheck is the root's own reporter (independent defect).
    reporter = next(n for n in graph["nodes"] if n["name"] == failing_check)
    assert reporter["status"] == STATUS_FAILED
    assert reporter["is_root"] is True
    assert reporter["root_receipt_id"] == graph["root"]["root_receipt_id"]


@pytest.mark.unit
def test_product_failed_is_occ_independent() -> None:
    # A real product defect surfaces as PRODUCT_FAILED even when OCC eligibility
    # is red — the two dimensions no longer collapse (the #1450/#1451 fix).
    subchecks = _green_subchecks()
    subchecks["tests"] = "failure"
    graph = build_reason_graph(
        {"head_sha": _HEAD, "subchecks": subchecks, "occ_eligibility": "failure"}
    )
    assert graph["root"]["kind"] == PRODUCT_FAILED
    assert graph["root"]["primary_signal"] == "tests=failure"


# --------------------------------------------------------------------------
# Green — single-node graph, freeze-eligible.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_green_all_pass_is_ready_single_node() -> None:  # green-all-pass-market
    graph = build_reason_graph({"head_sha": _HEAD, "subchecks": _green_subchecks()})
    assert graph["root"] is None
    assert graph["ready"] is True
    assert graph["freeze_eligible"] is True
    assert graph["blocked_candidate_count"] == 0
    assert graph["blocked_upstream_count"] == 0
    assert all(n["status"] != STATUS_BLOCKED_UPSTREAM for n in graph["nodes"])


# --------------------------------------------------------------------------
# EVIDENCE_MISSING cascade collapse — the CI-01 projection contract.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_evidence_missing_collapses_cascade_to_one_root() -> None:
    # occ-preflight red while all product checks are SKIPPED (the needs:
    # occ-preflight jobs that never ran). Exactly one EVIDENCE_MISSING root; the
    # M skipped checks are all BLOCKED_UPSTREAM under the SAME receipt id.
    subchecks = dict.fromkeys(
        ("change_detection", "lint", "typecheck", "tests", "coverage"), "skipped"
    )
    graph = build_reason_graph(
        {"head_sha": _HEAD, "subchecks": subchecks, "occ_eligibility": "failure"}
    )
    assert graph["root"]["kind"] == EVIDENCE_MISSING
    assert graph["blocked_candidate_count"] == 1  # not M
    receipt = graph["root"]["root_receipt_id"]
    dependents = [n for n in graph["nodes"] if n["status"] == STATUS_BLOCKED_UPSTREAM]
    assert len(dependents) == 5  # the five skipped product subchecks
    assert all(n["root_receipt_id"] == receipt for n in dependents)


@pytest.mark.unit
def test_absent_occ_input_does_not_fire_evidence_missing() -> None:
    # The product shadow deliberately does not consume OCC; an empty
    # occ_eligibility must NOT invent an EVIDENCE_MISSING root on a green head.
    graph = build_reason_graph(
        {"head_sha": _HEAD, "subchecks": _green_subchecks(), "occ_eligibility": ""}
    )
    assert graph["root"] is None
    assert graph["ready"] is True


# --------------------------------------------------------------------------
# Single-rooting precedence — deterministic arbitration.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_precedence_infra_and_api_outrank_product_and_evidence() -> None:
    subchecks = _green_subchecks()
    subchecks["lint"] = "failure"
    facts = {
        "head_sha": _HEAD,
        "subchecks": subchecks,
        "occ_eligibility": "failure",
        "policy": "prod-hold",
        "runner_signal": "disk-preflight",
        "gh_api": "5xx",
        "deploy_trigger": "failure",
    }
    graph = build_reason_graph(facts)
    # GITHUB_API_OUTAGE is highest precedence.
    assert graph["root"]["kind"] == GITHUB_API_OUTAGE

    del facts["gh_api"]
    assert build_reason_graph(facts)["root"]["kind"] == RUNNER_INFRA

    del facts["runner_signal"]
    assert build_reason_graph(facts)["root"]["kind"] == POLICY_HELD

    del facts["policy"]
    # occ red + an affirmative product failure -> product wins (EVIDENCE_MISSING
    # only fires when NO product check independently failed).
    assert build_reason_graph(facts)["root"]["kind"] == PRODUCT_FAILED

    facts["subchecks"]["lint"] = "success"
    # now no product failure; occ red -> EVIDENCE_MISSING.
    assert build_reason_graph(facts)["root"]["kind"] == EVIDENCE_MISSING


@pytest.mark.unit
def test_deploy_trigger_failed_is_lowest_precedence() -> None:
    graph = build_reason_graph(
        {
            "head_sha": _HEAD,
            "subchecks": _green_subchecks(),
            "deploy_trigger": "failure",
        }
    )
    assert graph["root"]["kind"] == DEPLOY_TRIGGER_FAILED


# --------------------------------------------------------------------------
# Replay determinism — identical head + facts => identical receipt id.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_replay_is_byte_identical() -> None:  # replay-determinism
    subchecks = _green_subchecks()
    subchecks["lint"] = "failure"
    facts = {"head_sha": _HEAD, "subchecks": subchecks}
    first = build_reason_graph(dict(facts))
    second = build_reason_graph(dict(facts))
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["root"]["root_receipt_id"] == second["root"]["root_receipt_id"]


@pytest.mark.unit
def test_receipt_id_is_head_and_kind_sensitive() -> None:
    a = root_receipt_id(_HEAD, PRODUCT_FAILED, "lint=failure")
    assert len(a) == 16
    assert a != root_receipt_id("b" * 40, PRODUCT_FAILED, "lint=failure")
    assert a != root_receipt_id(_HEAD, PRODUCT_FAILED, "tests=failure")
    assert a != root_receipt_id(_HEAD, EVIDENCE_MISSING, "lint=failure")


@pytest.mark.unit
def test_synchronize_new_head_supersedes_receipt() -> None:  # synchronize-new-head
    subchecks = _green_subchecks()
    subchecks["lint"] = "failure"
    old = build_reason_graph({"head_sha": "a" * 40, "subchecks": subchecks})
    new = build_reason_graph({"head_sha": "b" * 40, "subchecks": subchecks})
    # A new head SHA yields a distinct content-addressed receipt (not stale reuse).
    assert old["root"]["root_receipt_id"] != new["root"]["root_receipt_id"]


# --------------------------------------------------------------------------
# CLI surface — report-only, always exit 0.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_graph_is_report_only_exit_zero_on_red() -> None:
    facts = {"head_sha": _HEAD, "subchecks": {"lint": "failure"}}
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "graph", "--facts-json", json.dumps(facts)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["root"]["kind"] == PRODUCT_FAILED


# --------------------------------------------------------------------------
# ENFORCING CLI surface — `--exit-status` (OMN-14709, WS4 step 1).
#
# The load-bearing shadow-enforcement proof: a genuine PRODUCT_FAILED root exits
# NON-ZERO; a green graph and every NON-product root (RUNNER_INFRA /
# EVIDENCE_MISSING / GITHUB_API_OUTAGE / POLICY_HELD) stay exit 0. The workflow
# reason-graph step propagates this exit code on a NON-required context, so a red
# product defect reports without blocking the merge.
# --------------------------------------------------------------------------


def _run_graph_exit_status(facts: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "graph",
            "--facts-json",
            json.dumps(facts),
            "--exit-status",
        ],
        capture_output=True,
        text=True,
    )


@pytest.mark.unit
@pytest.mark.parametrize("failing_check", ["lint", "typecheck", "tests", "coverage"])
def test_exit_status_nonzero_on_product_failed_root(failing_check: str) -> None:
    """A genuine product defect (PRODUCT_FAILED root) exits NON-ZERO."""
    subchecks = _green_subchecks()
    subchecks[failing_check] = "failure"
    proc = _run_graph_exit_status({"head_sha": _HEAD, "subchecks": subchecks})
    assert proc.returncode == 1, proc.stdout
    assert json.loads(proc.stdout)["root"]["kind"] == PRODUCT_FAILED


@pytest.mark.unit
def test_exit_status_zero_on_green() -> None:
    """A fully green product dimension (READY, no root) stays exit 0."""
    proc = _run_graph_exit_status({"head_sha": _HEAD, "subchecks": _green_subchecks()})
    assert proc.returncode == 0, proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["root"] is None
    assert payload["ready"] is True


@pytest.mark.unit
def test_exit_status_zero_on_runner_infra_root() -> None:
    """A RUNNER_INFRA root (unconfirmable product dimension) stays exit 0."""
    subchecks = _green_subchecks()
    subchecks["tests"] = "cancelled"
    subchecks["coverage"] = "cancelled"
    proc = _run_graph_exit_status({"head_sha": _HEAD, "subchecks": subchecks})
    assert proc.returncode == 0, proc.stdout
    assert json.loads(proc.stdout)["root"]["kind"] == RUNNER_INFRA


@pytest.mark.unit
def test_exit_status_zero_on_evidence_missing_root() -> None:
    """An EVIDENCE_MISSING root (OCC red, product green) stays exit 0."""
    proc = _run_graph_exit_status(
        {
            "head_sha": _HEAD,
            "subchecks": _green_subchecks(),
            "occ_eligibility": "failure",
        }
    )
    assert proc.returncode == 0, proc.stdout
    assert json.loads(proc.stdout)["root"]["kind"] == EVIDENCE_MISSING


@pytest.mark.unit
@pytest.mark.parametrize(
    ("facts_extra", "expected_root"),
    [
        ({"gh_api": "5xx"}, GITHUB_API_OUTAGE),
        ({"policy": "prod-hold"}, POLICY_HELD),
    ],
)
def test_exit_status_zero_on_other_non_product_roots(
    facts_extra: dict, expected_root: str
) -> None:
    """GITHUB_API_OUTAGE and POLICY_HELD roots stay exit 0 (non-product)."""
    facts = {"head_sha": _HEAD, "subchecks": _green_subchecks(), **facts_extra}
    proc = _run_graph_exit_status(facts)
    assert proc.returncode == 0, proc.stdout
    assert json.loads(proc.stdout)["root"]["kind"] == expected_root


@pytest.mark.unit
def test_exit_status_zero_when_product_fail_masked_by_higher_root() -> None:
    """A product failure under a higher-precedence infra root is NOT fatal.

    Infra invalidates the observability of the product dimension, so the elected
    root is RUNNER_INFRA (not PRODUCT_FAILED) and the enforcing surface stays 0.
    """
    subchecks = _green_subchecks()
    subchecks["lint"] = "failure"
    proc = _run_graph_exit_status(
        {"head_sha": _HEAD, "subchecks": subchecks, "runner_signal": "disk-preflight"}
    )
    assert proc.returncode == 0, proc.stdout
    assert json.loads(proc.stdout)["root"]["kind"] == RUNNER_INFRA


# --------------------------------------------------------------------------
# STRUCTURAL — the shadow surface never couples to OCC (mints no OCC request).
# --------------------------------------------------------------------------


def _load_yaml(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.unit
def test_shadow_workflow_has_no_occ_preflight_in_needs_chain() -> None:
    wf = _load_yaml(_SHADOW_WF)
    jobs = wf["jobs"]
    for name, job in jobs.items():
        needs = job.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        assert "occ-preflight" not in needs, f"job {name} must not need occ-preflight"


@pytest.mark.unit
def test_shadow_workflow_never_triggers_occ_request() -> None:
    text = _SHADOW_WF.read_text(encoding="utf-8")
    # No EXECUTABLE reference to the OCC request minter (comments may name it for
    # documentation; what matters is that no `uses:` line invokes it).
    executable = [
        ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")
    ]
    assert not any("call-occ-preflight" in ln for ln in executable)
    assert not any("occ-preflight" in ln for ln in executable)
    # The only reusable it calls is the OCC-independent product-readiness scaffold.
    assert "uses: ./.github/workflows/product-readiness.yml" in text


@pytest.mark.unit
def test_reusable_product_readiness_is_occ_independent() -> None:
    wf = _load_yaml(_REUSABLE_WF)
    assert "workflow_call" in wf.get(
        True, wf.get("on", {})
    )  # PyYAML parses `on:` as True
    for name, job in wf["jobs"].items():
        needs = job.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        assert "occ-preflight" not in needs, f"reusable job {name} occ-coupled"
