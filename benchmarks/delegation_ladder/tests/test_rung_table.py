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
    partition_infra_failures,
    read_receipt_facts,
    rescore_record,
    rescored_record,
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


def test_a_row_the_ladder_moved_to_another_backend_is_never_accepted_for_the_pin() -> (
    None
):
    moved = _record("r-1", "review", "x", accepted=True)
    moved["backend_pin_honoured"] = False
    kept = _record("r-2", "review", "x", accepted=True)
    kept["backend_pin_honoured"] = True
    unknown = _record(
        "r-3", "review", "x", accepted=True
    )  # a harness row: no receipt field
    row = class_rung_table([moved, kept, unknown])[0]
    assert row["accepted"] == 2
    assert row["pin_moved"] == 1
    assert row["gate_refused_correct"] == 1


def test_only_plumbing_failures_are_run_again() -> None:
    def rec(kind: str | None, outcome: str) -> dict[str, object]:
        return {
            "task_id": kind or "ok",
            "failure_kind": kind,
            "scorer_outcome": outcome,
        }

    records = [
        rec("no_terminal_payload", "no_answer"),
        rec("client_timeout_420s", "no_answer"),
        rec("no_receipt", "no_answer"),
        rec("gate_reject:weak", "pass"),
        rec("scorer_fail", "fail"),
        rec(None, "pass"),
        rec("timeout", "no_answer"),  # a harness (model) timeout is the result
    ]
    stand, redo = partition_infra_failures(records)
    assert [r["task_id"] for r in redo] == [
        "no_terminal_payload",
        "client_timeout_420s",
        "no_receipt",
    ]
    assert [r["task_id"] for r in stand] == [
        "gate_reject:weak",
        "scorer_fail",
        "ok",
        "timeout",
    ]


def test_a_runtime_shut_down_mid_delegation_is_plumbing_but_a_model_failure_is_not() -> (
    None
):
    shutdown = {
        "task_id": "R4-01",
        "failure_kind": "status_failed",
        "scorer_outcome": "no_answer",
        "detail": "the runtime running this delegation shut down (redeploy, restart)",
    }
    model_error = {
        "task_id": "R4-02",
        "failure_kind": "status_failed",
        "scorer_outcome": "no_answer",
        "detail": "provider returned 500",
    }
    stand, redo = partition_infra_failures([shutdown, model_error])
    assert [r["task_id"] for r in redo] == ["R4-01"]
    assert [r["task_id"] for r in stand] == ["R4-02"]


def _scored(outcome: str, *, delivered: bool = True) -> dict[str, object]:
    accepted = delivered and outcome == "pass"
    record: dict[str, object] = {
        "task_id": "R4-06",
        "scorer_outcome": outcome,
        "score": 1.0 if outcome == "pass" else 0.0,
        "scorer_detail": "as first scored",
        "delivered": delivered,
        "accepted": accepted,
    }
    if delivered and outcome != "pass":
        record["failure_kind"] = f"scorer_{outcome}"
    return record


def test_a_rescored_cell_takes_the_new_verdict_and_keeps_the_first_one() -> None:
    got = rescored_record(_scored("fail"), "pass", 1.0, "now passes")
    assert got["scorer_outcome"] == "pass"
    assert got["accepted"] is True
    assert got["score"] == pytest.approx(1.0)
    assert got["scorer_detail"] == "now passes"
    assert "failure_kind" not in got
    assert got["first_scorer_outcome"] == "fail"
    assert got["first_scorer_detail"] == "as first scored"


def test_a_rescored_pass_that_now_fails_loses_its_acceptance() -> None:
    got = rescored_record(_scored("pass"), "fail", 0.0, "now fails")
    assert got["accepted"] is False
    assert got["failure_kind"] == "scorer_fail"
    assert got["first_scorer_outcome"] == "pass"


def test_a_rescored_pass_is_not_accepted_when_the_gate_refused_the_answer() -> None:
    got = rescored_record(_scored("fail", delivered=False), "pass", 1.0, "now passes")
    assert got["scorer_outcome"] == "pass"
    assert got["accepted"] is False


def test_an_unchanged_verdict_leaves_the_record_as_it_was() -> None:
    recorded = _scored("fail")
    assert rescored_record(recorded, "fail", 0.0, "as first scored") == recorded
    assert "first_scorer_outcome" not in rescored_record(recorded, "fail", 0.0, "x")


def test_a_cell_rescored_twice_keeps_the_very_first_verdict() -> None:
    once = rescored_record(_scored("fail"), "pass", 1.0, "p")
    twice = rescored_record(once, "fail", 0.0, "f")
    assert twice["first_scorer_outcome"] == "fail"


@pytest.mark.skipif(not hasattr(__import__("os"), "fork"), reason="POSIX fork runner")
def test_preloaded_probes_do_not_share_subject_imports_or_bytecode(
    tmp_path: Path,
) -> None:
    from benchmarks.delegation_ladder.run_ladder import _run_forked_pytest

    subject = tmp_path / "subject.py"
    test = tmp_path / "test_generated.py"
    test.write_text(
        "from subject import value\ndef test_value():\n assert value() == 1\n"
    )
    subject.write_text("def value(): return 1\n")
    assert _run_forked_pytest(tmp_path, test.name, 10)[0] == 0
    subject.write_text("def value(): return 2\n")
    assert _run_forked_pytest(tmp_path, test.name, 10)[0] == 1
    subject.write_text("def value(): return 1\n")
    assert _run_forked_pytest(tmp_path, test.name, 10)[0] == 0
    assert not list(tmp_path.glob("__pycache__/subject.*.pyc"))


@pytest.mark.skipif(not hasattr(__import__("os"), "fork"), reason="POSIX fork runner")
def test_preloaded_probe_keeps_the_timeout_and_reaps_its_child(tmp_path: Path) -> None:
    from benchmarks.delegation_ladder.run_ladder import _run_forked_pytest

    test = tmp_path / "test_generated.py"
    test.write_text("import time\ndef test_hangs():\n time.sleep(60)\n")
    assert _run_forked_pytest(tmp_path, test.name, 1) == (124, "timed out after 1s")
    test.write_text("def test_ok():\n assert True\n")
    assert _run_forked_pytest(tmp_path, test.name, 10)[0] == 0


def _rescore_input(tmp_path: Path) -> Path:
    response = "def test_vacuous():\n    assert True\n"
    saved = tmp_path / "responses" / "example" / "R4-01.json"
    saved.parent.mkdir(parents=True)
    saved.write_text(json.dumps({"response": response, "sha256": sha256_of(response)}))
    (tmp_path / "example.jsonl").write_text(
        json.dumps(
            {
                "task_id": "R4-01",
                "model_rung": "example",
                "response_sha256": sha256_of(response),
                "scorer_outcome": "pass",
                "delivered": True,
                "accepted": True,
            }
        )
        + "\n"
    )
    return saved


def test_parallel_rescore_retains_the_mutation_control_and_first_verdict(
    tmp_path: Path,
) -> None:
    from benchmarks.delegation_ladder.run_ladder import rescore_dir

    _rescore_input(tmp_path)
    assert rescore_dir(tmp_path) == 1
    assert rescore_dir(tmp_path, write=True) == 0
    record = json.loads((tmp_path / "example.jsonl").read_text())
    assert record["scorer_outcome"] == "fail"
    assert record["first_scorer_outcome"] == "pass"
    assert record["accepted"] is False
    assert "ALSO passes the mutant" in record["scorer_detail"]


def test_parallel_rescore_refuses_different_saved_bytes(tmp_path: Path) -> None:
    from benchmarks.delegation_ladder.run_ladder import rescore_dir

    saved = _rescore_input(tmp_path)
    blob = json.loads(saved.read_text())
    blob["response"] += "# changed after scoring\n"
    saved.write_text(json.dumps(blob))
    with pytest.raises(ValueError, match="saved response sha does not match"):
        rescore_dir(tmp_path)


def test_execution_reuse_requires_identical_source_and_expires_with_the_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmarks.delegation_ladder import run_ladder

    bundle = load_bundles(["R6-01"])[0].model_copy(
        update={
            "scorer_config": {
                "prelude_text": "",
                "prefix_body_text": "def value(): return 0\n",
                "focused_test_text": "from subject import value\ndef test_value():\n assert value() == 1\n",
            }
        }
    )
    executed: list[str] = []

    def execute(workdir: Path, test_name: str, timeout_s: int) -> tuple[int, str]:
        source = (workdir / "subject.py").read_text()
        executed.append(source)
        return (0 if "return 1" in source else 1), "one test ran"

    monkeypatch.setattr(run_ladder, "_run_forked_pytest", execute)
    cells = []
    for index, value in enumerate((1, 1, 2)):
        response = f"def value(): return {value}\n"
        saved = tmp_path / f"{index}.json"
        saved.write_text(
            json.dumps({"response": response, "sha256": sha256_of(response)})
        )
        cells.append((bundle, saved, {"response_sha256": sha256_of(response)}))
    for _ in range(2):
        verdicts = run_ladder._rescore_worker_group(cells)
        assert [verdict[0] for verdict in verdicts] == ["pass", "pass", "fail"]
    assert len(executed) == 6  # Three distinct programs, executed on each invocation.
