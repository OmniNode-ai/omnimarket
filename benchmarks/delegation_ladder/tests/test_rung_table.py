# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Behaviour tests for the per-class, per-rung result table (OMN-20844).

The table is what the benchmark report quotes, so it is tested the way the
scorers are: against hand-built records whose answer is known, with a negative
control for each claim it makes. A table that can only ever say "separated" (or
only ever say "overlap") would make the benchmark's conclusion a property of
the table, not of the models.

Vocabulary used here and in the module under test:

* ``rung`` on a ladder bundle is the task-complexity level (R1..R6).
* ``model_rung`` on a result record is the delegation rung that answered:
  the lab Qwen, a GLM model, a free OpenRouter model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.delegation_ladder.models import EnumOutcome, ModelTaskBundle
from benchmarks.delegation_ladder.run_ladder import (
    RUNGS,
    load_bundles,
    parse_onex_stdout,
    read_receipt_facts,
    rescore_record,
    rung_command,
)
from benchmarks.delegation_ladder.scorers import sha256_of
from benchmarks.delegation_ladder.summarise_results import (
    CLASS_ORDER,
    class_rung_table,
    class_separation,
    render_table,
    wilson_interval,
)

pytestmark = pytest.mark.unit

HERE = Path(__file__).resolve().parent.parent


def _record(
    task_id: str,
    task_type: str,
    model_rung: str,
    *,
    accepted: bool,
    delivered: bool = True,
    scorer_outcome: str | None = None,
    wall_ms: int = 10_000,
    model_latency_ms: int | None = 8_000,
    cost_usd: float | None = None,
) -> dict[str, object]:
    outcome = scorer_outcome or ("pass" if accepted else "fail")
    return {
        "task_id": task_id,
        "task_type": task_type,
        "model_rung": model_rung,
        "accepted": accepted,
        "delivered": delivered,
        "scorer_outcome": outcome,
        "wall_ms": wall_ms,
        "model_latency_ms": model_latency_ms,
        "cost_usd": cost_usd,
    }


def _batch(
    task_type: str,
    model_rung: str,
    passes: int,
    total: int,
    *,
    cost_usd: float | None = None,
) -> list[dict[str, object]]:
    return [
        _record(
            f"{task_type}-{i:02d}",
            task_type,
            model_rung,
            accepted=i < passes,
            cost_usd=cost_usd,
        )
        for i in range(total)
    ]


# --------------------------------------------------------------------------
# The interval
# --------------------------------------------------------------------------


def test_wilson_interval_matches_the_published_reference_values() -> None:
    # 20 of 20 and 0 of 20 are the textbook Wilson 95% cases.
    low, high = wilson_interval(20, 20)
    assert low == pytest.approx(0.839, abs=1e-3)
    assert high == pytest.approx(1.0, abs=1e-9)
    low, high = wilson_interval(0, 20)
    assert low == pytest.approx(0.0, abs=1e-9)
    assert high == pytest.approx(0.161, abs=1e-3)


def test_wilson_interval_of_nothing_is_the_whole_range_not_a_number() -> None:
    assert wilson_interval(0, 0) == (0.0, 1.0)


# --------------------------------------------------------------------------
# The table
# --------------------------------------------------------------------------


def test_one_row_per_class_and_rung_with_share_and_interval() -> None:
    records = _batch("review", "glm-5.3", 20, 25) + _batch("review", "qwen", 10, 25)
    rows = class_rung_table(records)
    by_key = {(r["task_type"], r["model_rung"]): r for r in rows}
    assert set(by_key) == {("review", "glm-5.3"), ("review", "qwen")}
    glm = by_key[("review", "glm-5.3")]
    assert (glm["tasks"], glm["accepted"]) == (25, 20)
    assert glm["accepted_share"] == pytest.approx(0.8)
    assert glm["interval_low"] < 0.8 < glm["interval_high"]
    low, high = wilson_interval(20, 25)
    assert (glm["interval_low"], glm["interval_high"]) == pytest.approx((low, high))


def test_rows_follow_the_class_order_the_report_uses() -> None:
    records = [
        *_batch("test", "a", 1, 2),
        *_batch("document", "a", 1, 2),
        *_batch("code_generation", "a", 1, 2),
    ]
    got = [r["task_type"] for r in class_rung_table(records)]
    assert got == [c for c in CLASS_ORDER if c in set(got)]
    assert got == ["document", "test", "code_generation"]


def test_a_task_that_got_no_answer_counts_against_the_rung_not_out_of_it() -> None:
    records = _batch("document", "x", 3, 4)
    records.append(
        _record(
            "document-99",
            "document",
            "x",
            accepted=False,
            delivered=False,
            scorer_outcome="no_answer",
            wall_ms=420_000,
            model_latency_ms=None,
        )
    )
    row = class_rung_table(records)[0]
    assert row["tasks"] == 5
    assert row["accepted"] == 3
    assert row["no_answer"] == 1
    assert row["accepted_share"] == pytest.approx(0.6)


def test_answer_correct_share_is_kept_apart_from_gate_accepted_share() -> None:
    # A correct answer the quality gate refused is a different fact from a wrong
    # answer: the benchmark reports both rather than folding one into the other.
    records = [
        _record("r-1", "review", "x", accepted=True),
        _record(
            "r-2", "review", "x", accepted=False, delivered=False, scorer_outcome="pass"
        ),
        _record("r-3", "review", "x", accepted=False),
    ]
    row = class_rung_table(records)[0]
    assert row["accepted"] == 1
    assert row["answer_correct"] == 2
    assert row["gate_refused_correct"] == 1


def test_median_latency_is_the_median_and_ignores_missing_receipts() -> None:
    records = [
        _record(
            "t-1", "test", "x", accepted=True, wall_ms=1_000, model_latency_ms=None
        ),
        _record(
            "t-2", "test", "x", accepted=True, wall_ms=9_000, model_latency_ms=4_000
        ),
        _record(
            "t-3", "test", "x", accepted=True, wall_ms=5_000, model_latency_ms=6_000
        ),
    ]
    row = class_rung_table(records)[0]
    assert row["median_wall_s"] == pytest.approx(5.0)
    assert row["median_model_latency_s"] == pytest.approx(5.0)
    assert row["model_latency_receipts"] == 2


def test_cost_is_reported_only_where_receipts_carry_it() -> None:
    priced = [
        _record("t-1", "test", "gem", accepted=True, cost_usd=0.01),
        _record("t-2", "test", "gem", accepted=True, cost_usd=0.02),
        _record("t-3", "test", "gem", accepted=False, cost_usd=None),
    ]
    unpriced = _batch("test", "glm", 2, 3, cost_usd=None)
    rows = {r["model_rung"]: r for r in class_rung_table(priced + unpriced)}
    assert rows["gem"]["cost_usd"] == pytest.approx(0.03)
    assert rows["gem"]["cost_receipts"] == 2
    # No receipt priced anything: the answer is "not measured", never zero.
    assert rows["glm"]["cost_usd"] is None
    assert rows["glm"]["cost_receipts"] == 0


def test_a_class_under_twenty_five_tasks_is_flagged_not_hidden() -> None:
    rows = class_rung_table(_batch("test", "x", 10, 24) + _batch("review", "x", 10, 25))
    flags = {r["task_type"]: r["meets_minimum_tasks"] for r in rows}
    assert flags == {"test": False, "review": True}


# --------------------------------------------------------------------------
# Separation
# --------------------------------------------------------------------------


def test_a_rung_whose_interval_clears_every_other_is_named_separated() -> None:
    records = (
        _batch("code_generation", "strong", 25, 25)
        + _batch("code_generation", "weak", 5, 25)
        + _batch("code_generation", "mid", 7, 25)
    )
    rows = class_rung_table(records)
    separation = class_separation(rows)["code_generation"]
    # `strong` clears both others; `weak` and `mid` overlap each other, so
    # neither of them is separated from *every* other rung.
    assert separation["separated"] == ["strong"]
    assert ("strong", "weak") in separation["disjoint_pairs"]
    assert ("strong", "mid") in separation["disjoint_pairs"]
    assert ("weak", "mid") not in separation["disjoint_pairs"]


def test_overlapping_intervals_separate_nobody() -> None:
    # 20/25 against 18/25: indistinguishable at n=25, which is the point of the
    # benchmark's larger sample, and exactly what a ranking must not hide.
    records = _batch("review", "a", 20, 25) + _batch("review", "b", 18, 25)
    separation = class_separation(class_rung_table(records))["review"]
    assert separation["separated"] == []
    assert separation["disjoint_pairs"] == []


def test_a_single_rung_class_has_nothing_to_be_separated_from() -> None:
    separation = class_separation(class_rung_table(_batch("test", "only", 25, 25)))
    assert separation["test"]["separated"] == []


def test_the_rendered_table_names_every_row_and_the_verdict_per_class() -> None:
    records = _batch("review", "strong", 25, 25) + _batch("review", "weak", 4, 25)
    rows = class_rung_table(records)
    text = render_table(rows, class_separation(rows))
    assert "review" in text
    assert "strong" in text
    assert "weak" in text
    assert "100%" in text
    assert "separated: strong" in text


# --------------------------------------------------------------------------
# The driver: rungs, one command per cell, one cell reproducible offline
# --------------------------------------------------------------------------


def test_every_rung_the_benchmark_runs_is_free_or_on_the_subscription() -> None:
    assert {
        "local-qwen3.8-27b",
        "glm-5.3-flash",
        "glm-5.3",
        "openrouter-nemotron-ultra",
        "openrouter-nemotron-super",
    } <= set(RUNGS)
    # Gemini is declared so a rerun can reach it, but it is never in the default
    # set: its free tier is capped at 20 requests per model.
    defaults = {name for name, rung in RUNGS.items() if rung.in_default_set}
    assert not {name for name in defaults if name.startswith("gemini")}
    assert {"glm-5.3", "glm-5.3-flash"} <= defaults


def test_a_pinned_onex_rung_never_lets_the_ladder_move_to_another_model() -> None:
    argv = rung_command(
        RUNGS["openrouter-nemotron-super"],
        prompt_file=Path("/scratch/p.txt"),
        task_type="review",
        ticket="OMN-20844",
        lane="lane-x",
        state_root=Path("/state"),
        onex=Path("/onex"),
        harness_script=Path("/h.py"),
    )
    assert argv[:3] == ["bash", "/onex", "delegate"]
    assert argv[argv.index("--backend-id") + 1] == "openrouter-nemotron-super"
    assert argv[argv.index("--task-type") + 1] == "review"
    assert "--prompt-file" in argv
    assert "--json" in argv


def test_a_glm_rung_goes_through_the_claude_code_harness_not_the_parked_backend() -> (
    None
):
    argv = rung_command(
        RUNGS["glm-5.3-flash"],
        prompt_file=Path("/scratch/p.txt"),
        task_type="document",
        ticket="OMN-20844",
        lane="lane-x",
        state_root=Path("/state"),
        onex=Path("/onex"),
        harness_script=Path("/h.py"),
    )
    assert argv[:2] == ["python3", "/h.py"]
    assert argv[argv.index("--harness") + 1] == "claude-glm"
    assert argv[argv.index("--model") + 1] == "glm-5.3-flash"
    assert "--backend-id" not in argv


def test_rescoring_a_saved_response_reproduces_the_recorded_cell(
    tmp_path: Path,
) -> None:
    bundle = next(b for b in load_bundles(None) if b.task_id == "R1-01")
    good = (
        "Mint a service-account token against dev.auth.omninode.ai using client "
        "onex-verify, then call the verify endpoint with the returned bearer."
    )
    saved = tmp_path / "R1-01.json"
    saved.write_text(
        json.dumps({"task_id": "R1-01", "sha256": sha256_of(good), "response": good})
    )
    recorded = {
        "task_id": "R1-01",
        "scorer_outcome": "pass",
        "response_sha256": sha256_of(good),
    }
    assert rescore_record(bundle, saved, recorded) == EnumOutcome.PASS.value

    # Negative control: the same cell with a wrong response does not reproduce.
    bad = good.replace("dev.auth.omninode.ai", "auth.omninode.ai")
    saved.write_text(
        json.dumps({"task_id": "R1-01", "sha256": sha256_of(bad), "response": bad})
    )
    with pytest.raises(ValueError, match="sha"):
        rescore_record(bundle, saved, recorded)


def test_the_task_set_meets_the_per_class_minimum() -> None:
    bundles: list[ModelTaskBundle] = load_bundles(None)
    counts: dict[str, int] = {}
    for bundle in bundles:
        counts[bundle.task_type] = counts.get(bundle.task_type, 0) + 1
    assert set(counts) == set(CLASS_ORDER)
    assert all(n >= 25 for n in counts.values()), counts


def _onex_doc(*, decision: str, response: str) -> str:
    attempt = {
        "backend_id": "openrouter-nemotron-super",
        "acceptance_decision": decision,
        "acceptance_reason": "quality_bar_met" if decision == "accept" else "weak",
    }
    payload = {
        "status": "completed",
        "response": response,
        "quality_gate_passed": decision == "accept",
        "attempts": [attempt],
        "metrics": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cost_usd": 0.0,
            "latency_ms": 1200,
        },
    }
    doc = {"run_id": "r-1", "result": {"terminal_payload": {"payload": payload}}}
    return "log line\n" + json.dumps(doc)


def test_an_accepted_onex_receipt_is_read_for_response_cost_and_latency() -> None:
    got = parse_onex_stdout(_onex_doc(decision="accept", response="ok"), Path("/s"))
    assert got["response"] == "ok"
    assert got["cost_usd"] == pytest.approx(0.0)
    assert got["model_latency_ms"] == 1200
    assert got["backend_id_answered"] == "openrouter-nemotron-super"
    assert "failure_kind" not in got


def test_a_gate_refused_onex_answer_keeps_its_text_but_is_not_delivered() -> None:
    got = parse_onex_stdout(_onex_doc(decision="reject", response="right"), Path("/s"))
    assert got["response"] == "right"
    assert got["failure_kind"] == "gate_reject:weak"


def test_a_run_with_no_receipt_names_what_the_cli_said_on_stderr() -> None:
    got = parse_onex_stdout("", Path("/s"), "Error: the clone is on a detached HEAD")
    assert got["failure_kind"] == "no_receipt"
    assert "detached HEAD" in got["detail"]


def test_receipt_facts_name_the_serving_endpoint_and_whether_the_pin_held(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps({"endpoint": "local", "backend_pin_honoured": True, "other": 1})
    )
    assert read_receipt_facts(str(receipt)) == {
        "endpoint": "local",
        "backend_pin_honoured": True,
    }
    # No receipt on this host (the run happened elsewhere): nothing is invented.
    assert read_receipt_facts(str(tmp_path / "missing.json")) == {}
    assert read_receipt_facts(None) == {}
