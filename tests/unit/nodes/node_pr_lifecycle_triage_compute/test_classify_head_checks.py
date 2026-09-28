# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``classify_head_checks`` classifier (OMN-19830, wave 2 task T8).

The corpus test is the acceptance criterion: the handler reproduces every
hand-labelled verdict in ``tests/fixtures/pr_landing/checks/2026-09-26/``,
with its per-check reason codes, its re-run list and the run attempt of every
blocking result it read (revision 1, change F7). The rule tests below pin each
rule on minimal facts, so a rule cannot pass only because the corpus happens
not to exercise its edge.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_head_checks import (
    HandlerClassifyHeadChecks,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_head_check_verdict import (
    HEAD_CHECK_RERUN_VERDICTS,
    EnumHeadCheckVerdict,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_head_check_facts import (
    ModelHeadCheckFacts,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_head_check_verdict import (
    ModelHeadCheckVerdict,
)

pytestmark = pytest.mark.unit

_CORPUS = (
    Path(__file__).resolve().parents[4]
    / "tests"
    / "fixtures"
    / "pr_landing"
    / "checks"
    / "2026-09-26"
)
_SHA = "0123456789abcdef0123456789abcdef01234567"
_T0 = "2026-09-26T10:00:00Z"
_V = EnumHeadCheckVerdict


def _corpus() -> list[tuple[Path, dict[str, Any]]]:
    return [(p, json.loads(p.read_text())) for p in sorted(_CORPUS.glob("*.json"))]


def _classify(facts: ModelHeadCheckFacts) -> ModelHeadCheckVerdict:
    return asyncio.run(HandlerClassifyHeadChecks().handle(facts))


# ----------------------------------------------------------------- the corpus


@pytest.mark.parametrize("path", sorted(_CORPUS.glob("*.json")), ids=lambda p: p.stem)
def test_the_classifier_reproduces_every_labelled_head(path: Path) -> None:
    name = path.name
    data = json.loads(path.read_text())
    facts = ModelHeadCheckFacts.model_validate(data["input"])
    expected = ModelHeadCheckVerdict.model_validate(data["expected"])
    got = _classify(facts)
    assert got.verdict is expected.verdict, (
        f"{name}: got {got.verdict}, labelled {expected.verdict} "
        f"({data['label']['rationale']})"
    )
    assert got == expected, f"{name}: {got.model_dump(mode='json')}"


def test_the_corpus_exercises_every_verdict() -> None:
    counts = Counter(d["expected"]["verdict"] for _, d in _corpus())
    missing = [v.value for v in EnumHeadCheckVerdict if counts[v.value] == 0]
    assert not missing, f"no labelled head exercises {missing}"


def test_change_control_open_stays_in_the_corpus() -> None:
    """R3: the reducer has a row for change_control_open, so the corpus keeps it."""
    counts = Counter(d["expected"]["verdict"] for _, d in _corpus())
    assert counts[_V.CHANGE_CONTROL_OPEN.value] >= 3


def test_every_corpus_verdict_carries_a_run_attempt_for_each_rerun_check() -> None:
    """F7: the reducer can only compare against expected_attempt if it has one."""
    for path, data in _corpus():
        verdict = _classify(ModelHeadCheckFacts.model_validate(data["input"]))
        attempted = {a.check for a in verdict.check_attempts}
        missing = set(verdict.rerun_checks) - attempted
        assert not missing, f"{path.name}: re-run checks with no attempt {missing}"


def test_classification_is_pure() -> None:
    _, data = _corpus()[0]
    facts = ModelHeadCheckFacts.model_validate(data["input"])
    before = facts.model_dump(mode="json")
    assert _classify(facts) == _classify(facts)
    assert facts.model_dump(mode="json") == before


# ------------------------------------------------------------ the rule tests


def _run(name: str, run_id: int, **overrides: Any) -> dict[str, Any]:
    check: dict[str, Any] = {
        "name": name,
        "check_run_id": run_id,
        "status": "completed",
        "conclusion": "success",
        "started_at": _T0,
        "completed_at": _T0,
        "app_slug": "github-actions",
        "run_id": 7000 + run_id,
        "run_attempt": 1,
        "failed_step": None,
        "required": True,
        "run_event": "pull_request",
        "workflow_path": ".github/workflows/ci.yml",
        "caller_changed_on_base": False,
        "annotations": [],
        "named_blockers": [],
    }
    check.update(overrides)
    return check


def _facts(*checks: dict[str, Any], **overrides: Any) -> ModelHeadCheckFacts:
    data: dict[str, Any] = {
        "repository": "OmniNode-ai/omnimarket",
        "pr_number": 1,
        "head_sha": _SHA,
        "base_ref": "dev",
        "observed_at": _T0,
        "checks": list(checks),
        "companion_state": "merged",
        "companion_pr": 11000,
        "merge_state": "blocked",
        "base_requires_up_to_date": False,
    }
    data.update(overrides)
    return ModelHeadCheckFacts.model_validate(data)


def _red(name: str, run_id: int, step: str, **overrides: Any) -> dict[str, Any]:
    return _run(name, run_id, conclusion="failure", failed_step=step, **overrides)


def test_all_blocking_checks_green_is_green() -> None:
    verdict = _classify(_facts(_run("lint", 1), _run("CI Summary", 2)))
    assert verdict.verdict is _V.GREEN
    assert verdict.rerun_checks == ()
    assert verdict.check_reasons == ()


def test_a_newer_green_copy_supersedes_an_older_red_one() -> None:
    older = _red("lint", 1, "Run ruff", started_at="2026-09-26T09:00:00Z")
    newer = _run("lint", 2, run_attempt=2)
    verdict = _classify(_facts(older, newer))
    assert verdict.verdict is _V.GREEN
    assert [(a.check, a.attempt) for a in verdict.check_attempts] == [("lint", 2)]


def test_a_non_required_red_that_nothing_names_does_not_block() -> None:
    verdict = _classify(
        _facts(_run("lint", 1), _red("docs", 2, "pytest", required=False))
    )
    assert verdict.verdict is _V.GREEN


def test_a_check_a_red_aggregate_names_blocks() -> None:
    summary = _red(
        "CI Summary",
        1,
        "Poll run jobs and compute fail-closed CI Summary verdict",
        named_blockers=["Tests (Split 1/2)"],
    )
    shard = _red("Tests (Split 1/2)", 2, "Run pytest (full suite)", required=False)
    verdict = _classify(_facts(summary, shard))
    assert verdict.verdict is _V.PRODUCT_FAILED
    assert verdict.rerun_checks == ()
    assert [r.name for r in verdict.check_reasons] == [
        "CI Summary",
        "Tests (Split 1/2)",
    ]


def test_a_running_blocking_check_is_pending() -> None:
    running = _run("lint", 1, status="in_progress", conclusion=None, completed_at=None)
    assert _classify(_facts(running)).verdict is _V.PENDING


def test_a_strict_base_with_the_head_behind_is_behind_required() -> None:
    facts = _facts(_run("lint", 1), merge_state="behind", base_requires_up_to_date=True)
    assert _classify(facts).verdict is _V.BEHIND_REQUIRED


def test_behind_on_a_base_that_is_not_strict_is_not_behind_required() -> None:
    facts = _facts(_run("lint", 1), merge_state="behind")
    assert _classify(facts).verdict is _V.GREEN


def test_a_red_preflight_with_the_companion_open_is_change_control_open() -> None:
    preflight = _red("occ-preflight / eligibility", 1, "Resolve Evidence-Source")
    facts = _facts(preflight, companion_state="open")
    verdict = _classify(facts)
    assert verdict.verdict is _V.CHANGE_CONTROL_OPEN
    assert verdict.rerun_checks == ()


def test_a_red_preflight_with_the_companion_merged_is_change_control_stale() -> None:
    preflight = _red("occ-preflight / eligibility", 1, "Resolve Evidence-Source")
    summary = _red(
        "CI Summary",
        2,
        "Poll run jobs and compute fail-closed CI Summary verdict",
        named_blockers=["occ-preflight / eligibility"],
    )
    verdict = _classify(_facts(preflight, summary))
    assert verdict.verdict is _V.CHANGE_CONTROL_STALE
    assert verdict.rerun_checks == ("CI Summary", "occ-preflight / eligibility")


def test_a_shard_refusal_behind_open_change_control_follows_it() -> None:
    preflight = _red("occ-preflight / eligibility", 1, "Resolve Evidence-Source")
    coverage = _red("Coverage Sweep Gate", 2, "Refuse when the test shards did not run")
    verdict = _classify(_facts(preflight, coverage, companion_state="open"))
    assert verdict.verdict is _V.CHANGE_CONTROL_OPEN


def test_a_shard_refusal_with_change_control_green_is_a_product_red() -> None:
    coverage = _red("Coverage Sweep Gate", 2, "Refuse when the test shards did not run")
    verdict = _classify(_facts(_run("occ-preflight / eligibility", 1), coverage))
    assert verdict.verdict is _V.PRODUCT_FAILED


def test_a_product_red_outranks_open_change_control() -> None:
    preflight = _red("occ-preflight / eligibility", 1, "Resolve Evidence-Source")
    tests = _red("Tests Gate", 2, "Run pytest")
    verdict = _classify(_facts(preflight, tests, companion_state="open"))
    assert verdict.verdict is _V.PRODUCT_FAILED


def test_an_unrecognised_failed_step_fails_closed_to_product_failed() -> None:
    """A red nothing recognises is never re-run blindly (P2): an agent looks."""
    verdict = _classify(_facts(_red("Domain Enforcement", 1, "Enforce SQL")))
    assert verdict.verdict is _V.PRODUCT_FAILED
    assert verdict.rerun_checks == ()


def test_a_red_whose_caller_changed_on_the_base_is_stale_caller_pin() -> None:
    summary = _red(
        "CI Summary",
        1,
        "Poll run jobs and compute fail-closed CI Summary verdict",
        caller_changed_on_base=True,
        annotations=[
            "CI Summary poll deadline (90m) reached with gating jobs still pending"
        ],
    )
    verdict = _classify(_facts(summary))
    assert verdict.verdict is _V.STALE_CALLER_PIN
    assert verdict.rerun_checks == ()


def test_the_ci_summary_poll_deadline_is_timed_out() -> None:
    summary = _red(
        "CI Summary",
        1,
        "Poll run jobs and compute fail-closed CI Summary verdict",
        run_attempt=2,
        annotations=[
            "CI Summary poll deadline (90m) reached with gating jobs still pending"
        ],
    )
    verdict = _classify(_facts(summary))
    assert verdict.verdict is _V.TIMED_OUT
    assert verdict.rerun_checks == ("CI Summary",)
    assert [(a.check, a.attempt) for a in verdict.check_attempts] == [("CI Summary", 2)]


def test_a_job_over_its_time_limit_is_timed_out() -> None:
    job = _run(
        "Tests Gate",
        1,
        conclusion="cancelled",
        annotations=["The job has exceeded the maximum execution time of 20m0s"],
    )
    assert _classify(_facts(job)).verdict is _V.TIMED_OUT


def test_a_timed_out_conclusion_is_timed_out() -> None:
    assert (
        _classify(_facts(_run("lint", 1, conclusion="timed_out"))).verdict
        is _V.TIMED_OUT
    )


def test_a_runner_setup_failure_is_runner_infra() -> None:
    verdict = _classify(_facts(_red("lint", 1, "Set up job")))
    assert verdict.verdict is _V.RUNNER_INFRA
    assert verdict.rerun_checks == ("lint",)


def test_an_image_pre_pull_failure_is_runner_infra() -> None:
    red = _red("Customer Path Boundary", 1, "Pre-pull the harness broker image")
    assert _classify(_facts(red)).verdict is _V.RUNNER_INFRA


def test_a_github_api_outage_is_runner_infra() -> None:
    red = _red("lint", 1, "Run gate", annotations=["502 Bad Gateway"])
    assert _classify(_facts(red)).verdict is _V.RUNNER_INFRA


def test_a_plain_cancellation_is_cancelled() -> None:
    verdict = _classify(_facts(_run("scan", 1, conclusion="cancelled")))
    assert verdict.verdict is _V.CANCELLED
    assert verdict.rerun_checks == ("scan",)


def test_a_process_gate_outside_change_control_is_not_rerun() -> None:
    """A version-bump refusal needs an artifact, not a re-run: an agent acts."""
    red = _red(
        "Release Identity Gate",
        1,
        "No code merged onto a published version without a bump",
    )
    verdict = _classify(_facts(red))
    assert verdict.verdict is _V.PRODUCT_FAILED
    assert verdict.rerun_checks == ()


def test_a_rerun_verdict_names_every_blocking_red_in_order() -> None:
    verdict = _classify(
        _facts(
            _red("b-lint", 1, "Set up job"), _run("a-scan", 2, conclusion="cancelled")
        )
    )
    assert verdict.verdict is _V.RUNNER_INFRA
    assert verdict.rerun_checks == ("a-scan", "b-lint")


def test_a_check_with_no_known_attempt_is_left_out_of_the_attempts() -> None:
    verdict = _classify(_facts(_run("lint", 1, run_attempt=None), _run("scan", 2)))
    assert [(a.check, a.attempt) for a in verdict.check_attempts] == [("scan", 1)]


@pytest.mark.parametrize("verdict", sorted(v.value for v in HEAD_CHECK_RERUN_VERDICTS))
def test_rerun_verdicts_are_the_only_ones_that_rerun(verdict: str) -> None:
    for _, data in _corpus():
        got = _classify(ModelHeadCheckFacts.model_validate(data["input"]))
        if got.verdict.value == verdict:
            assert got.rerun_checks
        elif got.verdict not in HEAD_CHECK_RERUN_VERDICTS:
            assert got.rerun_checks == ()


def test_observed_at_is_not_read_from_a_clock() -> None:
    facts = _facts(_run("lint", 1), observed_at=datetime(2000, 1, 1, tzinfo=UTC))
    assert _classify(facts).verdict is _V.GREEN
