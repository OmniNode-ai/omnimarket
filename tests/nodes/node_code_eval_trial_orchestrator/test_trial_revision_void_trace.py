# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24173: the trial driver stamps each try with its model revision, retries, closes void and keeps its trace.

Through the orchestrator handler on the in-memory bus (exemplar: ``test_orchestrator_golden_chain.py``): a fixture
endpoint answers each placed unit with the revision it reports for that try, and the scorer answers each score.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest
import yaml

from omnimarket.enums.enum_code_eval_void_reason import EnumCodeEvalVoidReason
from omnimarket.models.enum_code_score import (
    EnumCodeScoreSource,
    EnumCodeScoreVerdict,
)
from omnimarket.models.eval_item.model_eval_item_traced import (
    ModelEvalItemTracePart,
    assemble_trace,
)
from omnimarket.models.model_delegated_code_edit import (
    EnumCodeEditStatus,
    ModelCodeEditResult,
)
from omnimarket.models.model_lab_work_unit import (
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from tests.nodes.node_code_eval_trial_orchestrator.orchestrator_support import (
    REVISION,
    Bus,
    completed_host,
    leg_config,
    make_bus,
    pass_controls_only,
    run_request,
)

pytestmark = pytest.mark.unit

CONTRACT_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_code_eval_trial_orchestrator"
)

OTHER = "sha256:rev-b"
FIRST_TASK = "OMN-1000-"  # the task key of the first candidate


def host_reporting(revisions: list[str]):
    """A fixture endpoint: the nth placed unit reports ``revisions[n]`` (the last one stands after the list)."""
    placed: list[str] = []

    def host(unit: ModelLabWorkUnitRequest) -> ModelLabWorkUnitReceipt:
        placed.append(unit.work_unit_id)
        revision = revisions[min(len(placed), len(revisions)) - 1]
        return completed_host(
            unit,
            code_edit_result=ModelCodeEditResult(
                loop_run_id="loop-1",
                status=EnumCodeEditStatus.ACCEPTED,
                turns=1,
                model_revision=revision,
            ),
        )

    return host


def host_dropping_revision_of(task_key: str, times: int):
    """The endpoint reports no revision for the first ``times`` attempts at one task, a revision otherwise."""
    seen: defaultdict[str, int] = defaultdict(int)

    def host(unit: ModelLabWorkUnitRequest) -> ModelLabWorkUnitReceipt:
        assert unit.code_draft is not None
        key = task_key if task_key in unit.code_draft.task else ""
        seen[key] += 1
        missing = bool(key) and seen[key] <= times
        return completed_host(
            unit,
            code_edit_result=ModelCodeEditResult(
                loop_run_id="loop-1",
                status=EnumCodeEditStatus.ACCEPTED,
                turns=1,
                model_revision="" if missing else REVISION,
            ),
        )

    return host


def fail_tries(request) -> tuple[EnumCodeScoreVerdict, EnumCodeScoreSource | None]:
    return pass_controls_only(request)


def run(bus: Bus, n_tasks: int, n_arms: int, *, redeliver: bool = False) -> Bus:
    bus.send(run_request(n_tasks, n_arms))
    bus.drain(redeliver=redeliver)
    return bus


def labels(bus: Bus) -> list[tuple[str, ...]]:
    return [s.verdicts for s in bus.closes[0].sequences]


def test_revision_every_try_row_carries_the_revision_its_endpoint_reported() -> None:
    bus = run(make_bus(host=host_reporting([REVISION])), 1, 1)
    tries = [r for r in bus.rows if r.try_ordinal]
    assert tries
    assert {r.model_revision for r in tries} == {REVISION}
    assert bus.closes[0].void_reason is None
    assert bus.closes[0].model_revisions == {"model-0": REVISION}


def test_revision_changed_mid_run_ends_the_changed_tries_registration_mismatch_and_voids() -> (
    None
):
    # the first try reports rev-a (registered), every later one rev-b; every try scores fail_check
    bus = run(make_bus(host=host_reporting([REVISION, OTHER])), 1, 1)
    assert labels(bus) == [
        (
            "fail_check",
            "unscorable(registration_mismatch)",
            "unscorable(registration_mismatch)",
        )
    ]
    assert bus.closes[0].void_reason is EnumCodeEvalVoidReason.REGISTRATION_MISMATCH
    stamped = [r.model_revision for r in bus.rows if r.try_ordinal]
    assert stamped == [REVISION, OTHER, OTHER]


def test_revision_unobserved_when_the_response_carries_none() -> None:
    bus = run(make_bus(host=host_reporting([""])), 1, 1)
    assert labels(bus) == [("unscorable(revision_unobserved)",) * 3]
    assert [r.unscorable_source for r in bus.rows if r.try_ordinal] == [
        EnumCodeScoreSource.HARNESS
    ] * 3
    assert bus.closes[0].void_reason is (
        EnumCodeEvalVoidReason.INFRASTRUCTURE_UNSCORABLE_AFTER_RETRIES
    )
    assert bus.closes[0].model_revisions == {}


def test_retry_an_unobserved_revision_is_generated_again_and_keeps_its_ordinal() -> (
    None
):
    config = leg_config(infra_retries=2)
    bus = run(
        make_bus(config=config, host=host_dropping_revision_of(FIRST_TASK, 1)), 1, 1
    )
    # the retry scored: no try is left unscorable, but the one first try was (1 of 1 is over a tenth)
    assert bus.closes[0].void_reason is (
        EnumCodeEvalVoidReason.INFRASTRUCTURE_UNSCORABLE_FIRST_TRIES
    )
    assert labels(bus) == [("fail_check",) * 3]
    # one placement per attempt: the regenerated attempt is a unit of its own
    ids = [u.work_unit_id for u in bus.units]
    assert len(ids) == len(set(ids))
    first = [r for r in bus.rows if r.try_ordinal == 1]
    assert len(first) == 1, "the superseded attempt draws no result row"
    assert first[0].model_revision == REVISION


def test_retry_a_superseded_attempt_keeps_a_trace_and_no_result_row() -> None:
    config = leg_config(infra_retries=1)
    bus = make_bus(config=config, host=host_dropping_revision_of(FIRST_TASK, 1))
    run(bus, 1, 1)
    by_event: defaultdict[object, set[object]] = defaultdict(set)
    for part in bus.parts:
        by_event[part.event_id].add(part.scoring_id)
    # the first try (ordinal 1) was attempted twice: one event id, two scoring ids
    assert any(len(scorings) == 2 for scorings in by_event.values())


def test_void_when_eleven_percent_of_first_tries_were_infrastructure_unscorable() -> (
    None
):
    # nine sequences, one first try unobserved then retried and passed: 1 of 9 is 11 percent
    config = leg_config(infra_retries=2)
    bus = run(
        make_bus(config=config, host=host_dropping_revision_of(FIRST_TASK, 1)), 9, 1
    )
    closed = bus.closes[0]
    assert closed.void_reason is (
        EnumCodeEvalVoidReason.INFRASTRUCTURE_UNSCORABLE_FIRST_TRIES
    )
    assert not closed.abandoned


def test_void_not_when_ten_percent_of_first_tries_were_infrastructure_unscorable() -> (
    None
):
    # the positive control: 1 of 10 is exactly 10 percent, which is not more than 10 percent
    config = leg_config(infra_retries=2)
    bus = run(
        make_bus(config=config, host=host_dropping_revision_of(FIRST_TASK, 1)), 10, 1
    )
    assert bus.closes[0].void_reason is None


def test_void_when_a_try_is_still_unscorable_after_its_retries() -> None:
    config = leg_config(infra_retries=1)
    bus = run(
        make_bus(config=config, host=host_dropping_revision_of(FIRST_TASK, 99)), 2, 1
    )
    assert bus.closes[0].void_reason is (
        EnumCodeEvalVoidReason.INFRASTRUCTURE_UNSCORABLE_AFTER_RETRIES
    )
    # a retry is two attempts of one ordinal: 2 attempts x 3 ordinals for the unobserved task
    attempts = [
        u
        for u in bus.units
        if FIRST_TASK in (u.code_draft.task if u.code_draft else "")
    ]
    assert len(attempts) == 6


def test_void_a_redelivered_close_and_every_redelivered_answer_change_nothing() -> None:
    plain = run(make_bus(host=host_reporting([REVISION, OTHER])), 1, 1)
    again = run(make_bus(host=host_reporting([REVISION, OTHER])), 1, 1, redeliver=True)
    assert len(again.closes) == 1
    assert again.closes[0].void_reason is plain.closes[0].void_reason
    assert again.closes[0].sequences == plain.closes[0].sequences
    assert again.send(again.closes[0]) == []
    assert len(again.closes) == 1


def test_void_is_unset_when_every_try_is_scored_on_one_revision() -> None:
    bus = run(make_bus(host=host_reporting([REVISION])), 2, 2)
    assert bus.closes[0].void_reason is None


def assemble(parts: list[ModelEvalItemTracePart], scoring_id: object):
    return assemble_trace(tuple(p for p in parts if p.scoring_id == scoring_id))


def test_trace_every_try_has_one_with_its_checks_whole() -> None:
    output = "line of output\n" * 1000
    bus = make_bus(check_output=output)
    run(bus, 1, 1)
    scorings = {p.scoring_id for p in bus.parts}
    assert len(scorings) == 3, "one trace per try: the bound is 3, every try failed"
    for scoring in scorings:
        trace = assemble(bus.parts, scoring)
        assert [c.output for c in trace.checks] == [output]
        assert trace.checks[0].command == "uv run pytest tests/test_x.py"
        assert trace.checks[0].exit_code == 1
        assert trace.diff_text.startswith("diff of ")
        assert trace.input_text.startswith("fix it ")
        assert dict(trace.generation_settings)["model"] == "model-0"


def test_trace_a_three_megabyte_check_output_round_trips_through_its_parts() -> None:
    output = ("x" * 99 + "\n") * 30_000  # 3,000,000 characters
    assert len(output) == 3_000_000
    bus = make_bus(check_output=output)
    run(bus, 1, 1)
    scorings = {p.scoring_id for p in bus.parts}
    for scoring in scorings:
        parts = [p for p in bus.parts if p.scoring_id == scoring]
        assert len(parts) > 1, "a 3 MB trace goes as numbered parts"
        assert assemble_trace(tuple(parts)).checks[0].output == output


def test_trace_the_part_event_is_routed_to_the_traced_topic_by_the_contract() -> None:
    contract = yaml.safe_load(
        (CONTRACT_DIR / "contract.yaml").read_text(encoding="utf-8")
    )
    published = {e["event_type"]: e["topic"] for e in contract["published_events"]}
    name = ModelEvalItemTracePart.__name__.removeprefix("Model")
    assert name in published
    assert published[name] in contract["event_bus"]["publish_topics"]
