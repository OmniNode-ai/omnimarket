# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Escalating-complexity graded ladder benchmark proof (OMN-13935, plan §3.6).

Supersedes the earlier fixture-content smoke replay. These tests grade GENUINE
recorded per-rung model outputs (durable evidence captured from the live local
ladder) and assert the operator's acceptance criterion: the benchmark SEPARATES
the ladder — the floor rung scores measurably below the ceiling rung. The
separation is a real capability signal, not a scoring artifact, because every
grader is objective (numeric equality / substring / HumanEval-style code exec).
"""

from __future__ import annotations

import json
import os

import pytest

from omnimarket.delegation.graded_ladder.graders import (
    extract_answer,
    extract_code_block,
    grade,
    run_code_asserts,
)
from omnimarket.delegation.graded_ladder.harness import (
    DEFAULT_FIXTURES_PATH,
    GradedLadderHarness,
    build_benchmark_packet,
    load_corpus,
    load_recorded_outputs,
    load_rungs,
)
from omnimarket.delegation.graded_ladder.models import (
    EnumBenchmarkTier,
    EnumGraderKind,
    ModelLadderTask,
)
from scripts.ci.run_delegation_graded_benchmark import main as report_main

# ---------------------------------------------------------------------------
# Extraction + objective graders
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_strip_reasoning_and_boxed_numeric() -> None:
    task = ModelLadderTask(
        task_id="t",
        benchmark_tier=EnumBenchmarkTier.EASY,
        task_class="reasoning",
        prompt="x",
        grader=EnumGraderKind.NUMERIC,
        expected_number=376,
    )
    # <think> scratchpad + \boxed{} final answer.
    passed, _ = grade(task, "<think>47*8 = 376, let me check</think> \\boxed{376}")
    assert passed
    # A wrong final number must fail even if 376 appears in the scratchpad.
    fail_passed, _ = grade(task, "<think>376?</think> The answer is 375.")
    assert not fail_passed


@pytest.mark.unit
def test_extract_answer_drops_dangling_think() -> None:
    assert extract_answer("<think>reasoning with no close tag") == ""
    assert extract_answer("<think>a</think>Au") == "Au"


@pytest.mark.unit
def test_contains_grader_case_sensitivity() -> None:
    task = ModelLadderTask(
        task_id="t",
        benchmark_tier=EnumBenchmarkTier.EASY,
        task_class="research",
        prompt="x",
        grader=EnumGraderKind.CONTAINS,
        expected_substring="Au",
        case_sensitive=True,
    )
    assert grade(task, "Au")[0]
    assert not grade(task, "au")[0]


@pytest.mark.unit
def test_code_exec_grader_passes_correct_and_fails_wrong() -> None:
    good = "```python\nimport re\ndef is_palindrome(s):\n    t = re.sub(r'[^a-z0-9]','',s.lower())\n    return t == t[::-1]\n```"
    bad = "```python\ndef is_palindrome(s):\n    return s == s[::-1]\n```"
    task = ModelLadderTask(
        task_id="t",
        benchmark_tier=EnumBenchmarkTier.MEDIUM,
        task_class="code_generation",
        prompt="x",
        grader=EnumGraderKind.CODE_EXEC,
        entrypoint="is_palindrome",
        code_asserts=(
            "assert is_palindrome('A man, a plan, a canal: Panama') is True\n"
            "assert is_palindrome('abc') is False\n"
        ),
    )
    assert grade(task, good)[0]
    assert not grade(task, bad)[0]


@pytest.mark.unit
def test_code_exec_missing_entrypoint_fails_closed() -> None:
    ok, detail = run_code_asserts(
        "def other():\n    return 1\n",
        entrypoint="is_palindrome",
        asserts="assert is_palindrome('') is True\n",
    )
    assert not ok
    assert "entrypoint" in detail


@pytest.mark.unit
def test_extract_code_block_prefers_last_fence() -> None:
    text = "```python\nx=1\n```\nthen\n```python\ndef f():\n    return 2\n```"
    assert "def f()" in extract_code_block(text)


# ---------------------------------------------------------------------------
# Corpus / rung structure
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_corpus_spans_escalating_tiers() -> None:
    tasks = load_corpus()
    tiers = {t.benchmark_tier for t in tasks}
    assert tiers == set(EnumBenchmarkTier), "corpus must span easy/medium/hard"
    assert len(tasks) >= 6


@pytest.mark.unit
def test_ladder_includes_5090_and_4090_ai_pc_rungs() -> None:
    rungs = load_rungs()
    gpus = {r.gpu for r in rungs}
    assert "rtx_4090" in gpus, "operator §3.6 requires the 4090 AI-PC rung"
    assert "rtx_5090" in gpus, "operator §3.6 requires the 5090 AI-PC rung"
    # Rung `order` is a floor->ceiling permutation (contiguous, no dupes).
    orders = sorted(r.order for r in rungs)
    assert orders == list(range(len(rungs)))
    # No committed site-specific host/IP or local path (portability rule #6).
    # Public cloud https endpoints are allowed; private IPs / tailscale / paths
    # are not.
    forbidden = ("192.168.", "100.99.", "100.109.", ".ts.net", "/users/", "localhost")  # test-literal-ok: guard corpus for forbidden rung endpoint tokens
    for r in rungs:
        blob = r.model_dump_json().lower()
        for bad in forbidden:
            assert bad not in blob, f"{r.rung_id} embeds forbidden token {bad!r}"


@pytest.mark.unit
def test_ladder_includes_paid_cloud_ceiling() -> None:
    """Operator correction: paid-cloud is NOT deferred — cloud rungs must exist.

    The ladder must include the GLM paid-cloud rung (z.ai) and an OpenRouter
    free-tier rung, both carrying a public endpoint + a Bearer api_key_env.
    """

    rungs = {r.rung_id: r for r in load_rungs()}
    glm = rungs.get("rung_cloud_glm")
    orf = rungs.get("rung_openrouter_free")
    assert glm is not None
    assert glm.model_name == "glm-5.2"
    assert glm.endpoint_url.startswith("https://")
    assert glm.api_key_env
    assert orf is not None
    assert ":free" in orf.model_name.lower()
    assert orf.endpoint_url.startswith("https://openrouter.ai")
    assert orf.api_key_env
    # The paid-cloud GLM rung is the ceiling (top order) — the true frontier tier.
    ceiling = max(load_rungs(), key=lambda r: r.order)
    assert ceiling.rung_id == "rung_cloud_glm"


@pytest.mark.unit
def test_cloud_rungs_recorded_genuine_content() -> None:
    """Both cloud rungs carry REAL recorded content (not stubs)."""

    recorded = load_recorded_outputs()["rungs"]
    for rung_id in ("rung_cloud_glm", "rung_openrouter_free"):
        cells = recorded.get(rung_id)
        assert cells, f"no recorded cloud content for {rung_id}"
        got_200 = [c for c in cells.values() if c.get("http_status") == 200]
        assert got_200, f"{rung_id} has no successful (200) recorded cell"
        assert any(c.get("content") for c in got_200), (
            f"{rung_id} recorded no non-empty content"
        )


# ---------------------------------------------------------------------------
# Acceptance: floor < ceiling separation on the recorded ladder
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recorded_fixtures_cover_every_rung_and_task() -> None:
    """Every (rung, task) cell is recorded; timeout cells are legit budget fails.

    A non-200 cell is a genuine "the rung did not deliver within the recording
    timeout" fail (the local reasoner's real behavior — production gives
    local-reasoner a 60s budget and escalates on timeout). Such cells MUST carry
    empty content so the objective grader scores them as a fail rather than
    silently passing on stale text.
    """

    rungs = load_rungs()
    tasks = load_corpus()
    recorded = load_recorded_outputs()
    for rung in rungs:
        rung_recs = recorded["rungs"].get(rung.rung_id)
        assert rung_recs is not None, f"no recorded outputs for {rung.rung_id}"
        for task in tasks:
            assert task.task_id in rung_recs, f"{rung.rung_id} missing {task.task_id}"
            cell = rung_recs[task.task_id]
            status = cell.get("http_status")
            if status != 200:
                assert not cell.get("content"), (
                    f"{rung.rung_id}/{task.task_id}: non-200 cell must be empty"
                )


@pytest.mark.unit
def test_floor_rung_has_genuine_capability_ceiling() -> None:
    """The floor rung is genuinely weaker: it must miss at least one hard task.

    Guards against a corpus so easy that every rung saturates — separation would
    then be an artifact. The recorded floor rung (4090 Qwen3.6-27B) fails the
    hardest tasks (timeout and/or wrong answer), which is exactly what produces
    a real capability gradient.
    """

    packet = build_benchmark_packet()
    floor = min(packet.rung_scores, key=lambda s: s.order)
    assert floor.tasks_passed < floor.tasks_total, "floor rung must not be perfect"
    assert floor.per_tier_pass_rate.get("hard", 1.0) < 1.0


@pytest.mark.unit
def test_graded_benchmark_separates_floor_from_ceiling() -> None:
    """THE acceptance gate: floor scores measurably below ceiling."""

    packet = build_benchmark_packet()
    assert packet.passed, packet.failures
    assert packet.ticket == "OMN-13935"

    sep = packet.separation
    assert sep is not None
    # Real separation, not a flat pass/fail: ceiling strictly exceeds floor by
    # at least the required margin.
    assert sep.ceiling_score > sep.floor_score
    assert sep.margin >= sep.required_margin
    assert sep.separated

    scores = {s.rung_id: s for s in packet.rung_scores}
    floor = scores[sep.floor_rung_id]
    ceiling = scores[sep.ceiling_rung_id]
    # Sanity: the ceiling clears the hard tier better than the floor — this is
    # where genuine capability diverges.
    assert ceiling.per_tier_pass_rate.get("hard", 0.0) >= floor.per_tier_pass_rate.get(
        "hard", 0.0
    )


@pytest.mark.unit
def test_weighted_scores_are_monotonic_nondecreasing() -> None:
    packet = build_benchmark_packet()
    ordered = sorted(packet.rung_scores, key=lambda s: s.order)
    weighted = [s.weighted_score for s in ordered]
    assert weighted == sorted(weighted), (
        f"ladder weighted scores must not regress floor->ceiling: {weighted}"
    )


@pytest.mark.unit
def test_missing_cell_fails_closed() -> None:
    rungs = load_rungs()
    tasks = load_corpus()
    recorded = load_recorded_outputs()
    # Drop one recorded cell -> harness must fail closed, not silently pass.
    dropped = json.loads(json.dumps(recorded))
    a_rung = rungs[0].rung_id
    a_task = tasks[0].task_id
    dropped["rungs"][a_rung].pop(a_task, None)
    harness = GradedLadderHarness(rungs, tasks, dropped)
    packet = harness.build_packet()
    assert not packet.passed
    assert any("no recorded output" in f for f in packet.failures)


@pytest.mark.unit
def test_report_main_returns_zero() -> None:
    assert report_main([]) == 0


@pytest.mark.unit
def test_fixture_source_is_committed() -> None:
    assert DEFAULT_FIXTURES_PATH.exists()
    packet = build_benchmark_packet()
    assert packet.fixture_source.endswith("recorded_rung_outputs.json")


# ---------------------------------------------------------------------------
# Live re-record path (skipped in CI; hermetic fixtures are the CI source)
# ---------------------------------------------------------------------------


@pytest.mark.live_model
@pytest.mark.skipif(
    os.environ.get("OMN_ALLOW_LIVE_LADDER") != "1",
    reason="live local-ladder capture; set OMN_ALLOW_LIVE_LADDER=1 to enable",
)
def test_live_recorder_captures_at_least_one_rung() -> None:
    """Exercise the live recorder against the real ladder (opt-in only).

    CI never runs this — the committed recorded fixtures are the deterministic
    source. This proves the capture path still resolves an endpoint and returns
    a graded-benchmark-shaped result when explicitly enabled on a LAN host.
    """

    import tempfile
    from pathlib import Path

    from scripts.ci.record_delegation_ladder_fixtures import main as record_main

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "live_capture.json"
        rc = record_main(["--out", str(out), "--max-tokens", "512", "--timeout", "300"])
        captured = json.loads(out.read_text())
        assert captured["rungs"], "no rung captured from the live ladder"
        # exit 0 only when every configured rung resolved and recorded.
        assert rc in (0, 1)
