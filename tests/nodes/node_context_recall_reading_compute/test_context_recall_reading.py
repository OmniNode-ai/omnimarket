# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Piece 16's reading flips on its two faults (OMN-22980).

AC1 (-k planted_fact): a planted fact recalled after a forced compaction reads WORKING; a
non-recall, or a compaction that was not forced or did not finish, reads NOT WORKING.
AC2 (-k eval_without_result): an eval run that ends with no result, or never ends inside its
deadline, reads NOT WORKING.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from omnimarket.models.model_context_recall_reading import (
    ModelContextRecallOverlay,
    ModelContextRecallReading,
)
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_context_recall_reading_compute.handlers.handler_context_recall_reading import (
    HandlerContextRecallReading,
)
from tests.nodes.node_context_recall_reading_compute.reading_support import (
    eval_run,
    overlay,
    overlay_data,
    probe,
    request,
)

pytestmark = pytest.mark.unit

WORKING = EnumReadingVerdict.WORKING
NOT_WORKING = EnumReadingVerdict.NOT_WORKING
UNKNOWN = EnumReadingVerdict.UNKNOWN


def read(probes=None, eval_runs=None) -> ModelContextRecallReading:  # type: ignore[no-untyped-def]
    return HandlerContextRecallReading().handle(request(probes, eval_runs))


def test_planted_fact_recalled_after_forced_compaction_reads_working() -> None:
    reading = read()
    assert reading.verdict is WORKING
    assert reading.reasons == []
    assert reading.probes_recalled == 1
    assert reading.eval_runs_with_result == 1


def test_planted_fact_not_recalled_flips_to_not_working() -> None:
    reading = read([probe(recalled_answer="I do not have that.")])
    assert reading.verdict is NOT_WORKING
    assert reading.reasons == ["planted-fact-not-recalled:p1"]
    assert reading.probes_recalled == 0
    assert reading.probe_reads[0].recalled is False


def test_planted_fact_no_answer_flips_to_not_working() -> None:
    reading = read([probe(recalled_answer=None)])
    assert reading.verdict is NOT_WORKING
    assert reading.reasons == ["planted-fact-not-recalled:p1"]


def test_planted_fact_recall_is_exact_not_fuzzy() -> None:
    """A near miss of the fact is a non-recall: the match is the planted text, never a partial."""
    reading = read([probe(recalled_answer="the launch codeword is heron-4417")])
    assert reading.verdict is NOT_WORKING


def test_planted_fact_without_a_forced_compaction_flips_to_not_working() -> None:
    reading = read([probe(compaction_forced=False, compaction_done=False)])
    assert reading.verdict is NOT_WORKING
    assert reading.reasons == ["compaction-not-completed:p1"]


def test_planted_fact_compaction_started_but_not_finished_flips_to_not_working() -> (
    None
):
    reading = read([probe(compaction_done=False)])
    assert reading.verdict is NOT_WORKING
    assert reading.reasons == ["compaction-not-completed:p1"]


def test_planted_fact_asked_before_the_compaction_finished_is_out_of_order() -> None:
    reading = read([probe(planted=1800, compacted=300, read=600)])
    assert reading.verdict is NOT_WORKING
    assert reading.reasons == ["probe-out-of-order:p1"]


def test_planted_fact_one_failure_among_passing_probes_flips_the_reading() -> None:
    reading = read([probe("p1"), probe("p2", recalled_answer="no idea"), probe("p3")])
    assert reading.verdict is NOT_WORKING
    assert reading.reasons == ["planted-fact-not-recalled:p2"]
    assert reading.probes_recalled == 2


def test_planted_fact_older_than_the_max_age_is_stale() -> None:
    old = overlay().max_age_seconds + 60
    reading = read([probe(planted=old + 600, compacted=old + 300, read=old)])
    assert reading.verdict is NOT_WORKING
    assert reading.reasons == ["stale-probe:no-probe-in-window"]


def test_planted_fact_future_dated_is_not_working() -> None:
    reading = read([probe(planted=-10, compacted=-20, read=-30)])
    assert reading.verdict is NOT_WORKING
    assert any(r.startswith("future-dated:p1") for r in reading.reasons)


def test_planted_fact_none_supplied_reads_unknown() -> None:
    reading = read([], [eval_run()])
    assert reading.verdict is UNKNOWN
    assert reading.reasons == ["no-probe-supplied"]


def test_planted_fact_below_the_overlay_minimum_is_not_working() -> None:
    data = overlay_data()
    data["min_probes"] = 2
    raised = ModelContextRecallOverlay.model_validate(data)
    req = request().model_copy(update={"overlay": raised})
    reading = HandlerContextRecallReading().handle(req)
    assert reading.verdict is NOT_WORKING
    assert reading.reasons == ["below-minimum:probes:1-of-2"]


def test_eval_without_result_run_ended_with_no_result_flips_to_not_working() -> None:
    reading = read(eval_runs=[eval_run(result_id=None)])
    assert reading.verdict is NOT_WORKING
    assert reading.reasons == ["eval-ended-without-result:e1"]
    assert reading.eval_runs_with_result == 0


def test_eval_without_result_run_open_past_its_deadline_flips_to_not_working() -> None:
    late = overlay().eval_run_deadline_seconds + 60
    reading = read(eval_runs=[eval_run(started=late, ended=None, result_id=None)])
    assert reading.verdict is NOT_WORKING
    assert reading.reasons == ["eval-open-past-deadline:e1"]


def test_eval_without_result_among_good_runs_flips_the_reading() -> None:
    reading = read(
        eval_runs=[eval_run("e1"), eval_run("e2", result_id=None), eval_run("e3")]
    )
    assert reading.verdict is NOT_WORKING
    assert reading.reasons == ["eval-ended-without-result:e2"]
    assert reading.eval_runs_with_result == 2


def test_eval_without_result_none_supplied_reads_unknown() -> None:
    reading = read(eval_runs=[])
    assert reading.verdict is UNKNOWN
    assert reading.reasons == ["no-eval-run-supplied"]


def test_eval_without_result_only_in_progress_runs_do_not_meet_the_minimum() -> None:
    """A run still inside its deadline is neither a failure nor a result."""
    reading = read(eval_runs=[eval_run(started=60, ended=None, result_id=None)])
    assert reading.verdict is NOT_WORKING
    assert reading.reasons == ["below-minimum:eval-runs:0-of-1"]
    assert reading.eval_run_reads[0].in_progress is True


def test_eval_run_in_progress_beside_a_finished_run_does_not_flip() -> None:
    reading = read(
        eval_runs=[
            eval_run("e1"),
            eval_run("e2", started=60, ended=None, result_id=None),
        ]
    )
    assert reading.verdict is WORKING


def test_eval_without_result_run_older_than_the_max_age_is_stale() -> None:
    old = overlay().max_age_seconds + 600
    reading = read(eval_runs=[eval_run(started=old + 60, ended=old)])
    assert reading.verdict is NOT_WORKING
    assert reading.reasons == ["stale-eval-run:no-eval-run-in-window"]


def test_a_result_with_no_end_is_refused_at_the_model() -> None:
    with pytest.raises(ValueError, match="ended"):
        request(eval_runs=[eval_run(ended=None, result_id="r")])


def test_overlay_hash_is_a_full_sha256_of_the_canonical_overlay() -> None:
    reading = read()
    canonical = json.dumps(overlay_data(), sort_keys=True, separators=(",", ":"))
    assert reading.overlay_hash == hashlib.sha256(canonical.encode()).hexdigest()
    assert len(reading.overlay_hash) == 64


def test_overlay_example_states_every_threshold_and_the_model_has_no_defaults() -> None:
    assert set(overlay_data()) == set(ModelContextRecallOverlay.model_fields)
    with pytest.raises(ValueError, match="eval_run_deadline_seconds"):
        ModelContextRecallOverlay.model_validate({"max_age_seconds": 1})
