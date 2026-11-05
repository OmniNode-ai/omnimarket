# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fixed model's three continuations feed the existing action classifier."""

from __future__ import annotations

from pathlib import Path

from omnimarket.models.compaction_measurement.action import (
    EnumOpenLoopKind,
    ModelCallClassPolicy,
)
from omnimarket.models.compaction_measurement.boundary import ModelTranscriptCase
from omnimarket.models.compaction_measurement.ground_truth import (
    EnumExpectedConduct,
    EnumGroundTruthKind,
    ModelGroundTruthItem,
    ModelGroundTruthResult,
    ModelGroundTruthRow,
)
from omnimarket.nodes.node_compaction_continuation_replay_effect.container.container_replay import (
    create_replay_container,
)
from omnimarket.nodes.node_compaction_continuation_replay_effect.handlers.handler_experiment import (
    HandlerReplayExperiment,
)
from omnimarket.nodes.node_compaction_continuation_replay_effect.models import (
    ModelToolCall,
)
from omnimarket.nodes.node_compaction_continuation_replay_effect.models.model_experiment import (
    ModelAssistantTurn,
    ModelExperimentRequest,
    ModelReplayContext,
)


def context(boundary: str, arm: str) -> ModelReplayContext:
    case = ModelTranscriptCase(
        case_key=boundary,
        session_id="session",
        boundary_uuid=boundary,
        transcript_path="transcript.jsonl",
        trigger="auto",
        pre_tokens=100,
        post_tokens=10,
        boundary_at="2026-11-02T10:00:00Z",
        boundary_sha256="a" * 64,
        summary_record_sha256="b" * 64,
        summary_length=7,
        summary_sha256="c" * 64,
        summary_check="PINNED",
        prefix_bytes=10,
        prefix_sha256="d" * 64,
        prefix_records=1,
        prefix_bundle_path="bundle/prefix.jsonl",
        boundary_bundle_path="bundle/boundary.json",
        summary_record_bundle_path="bundle/summary.json",
        ledger_cuts=(),
        checkpoint={"status": "UNAVAILABLE", "reason": "fixture"},
        lane_attribution={"lanes": ["worker"], "rule": "fixture", "evidence": ()},
    )
    truth = ModelGroundTruthResult(
        session_id="session",
        now="2026-11-02T10:00:00Z",
        window_start="2026-11-02T09:00:00Z",
        lane_family=("worker",),
        prefix_row_count=1,
        rows_as_of_count=1,
        refused_row_count=0,
        prefix_hash="e" * 64,
        set_hash="f" * 64,
        counts_by_kind=(),
        items=(
            ModelGroundTruthItem(
                kind=EnumGroundTruthKind.OPEN_CLAIM,
                key="worker",
                quote="open worker",
                required_spans=("worker",),
                expected_conduct=EnumExpectedConduct.TRACK_TO_TERMINAL,
                rows=(
                    ModelGroundTruthRow(
                        stamp="2026-11-02T09:00:00Z",
                        row_type="CLAIM",
                        row_id="0" * 64,
                        role="primary",
                    ),
                ),
            ),
        ),
    )
    return ModelReplayContext(case=case, ground_truth=truth, arm=arm, context="summary")


def request(tmp_path: Path) -> ModelExperimentRequest:
    data = tmp_path / "cm14.yaml"
    data.write_text("jaccard_tolerance: 0.8\nmin_repeats: 3\n")
    return ModelExperimentRequest(
        correlation_id="run",
        contexts=[context(b, a) for b in ("b1", "b2") for a in ("stock_rerun", "mod")],
        n_turns=2,
        answering_model_id="fixed-answerer",
        continuation_prompt="Continue the work.",
        tolerance_file=str(data),
        policy=ModelCallClassPolicy(
            progress_classes={EnumOpenLoopKind.OPEN_CLAIM: ("onex.lane.relaunch",)}
        ),
    )


class ModelFixture:
    def __init__(self, drift: bool = False) -> None:
        self.inputs = []
        self.drift = drift

    def continue_turn(self, turn):
        self.inputs.append(turn)
        if turn.turn == 1:
            command = "onex lane relaunch worker"
            if (
                self.drift
                and turn.boundary_id == "b1"
                and turn.arm == "mod"
                and turn.repeat_index == 2
            ):
                command = "gh pr merge worker"
            return ModelAssistantTurn(
                text="resume",
                calls=(ModelToolCall(tool="bash", command=command, item_key="worker"),),
            )
        assert turn.history[-1].calls[0].refusal_reason is not None
        return ModelAssistantTurn(text="No further calls.")


def test_model_runs_n_turns_three_repeats_and_cm3_grades_two_boundaries(
    tmp_path: Path,
) -> None:
    model = ModelFixture()
    container = create_replay_container(
        model=model, scratch_root=tmp_path / "scratch", input_roots=(tmp_path,)
    )
    result = HandlerReplayExperiment(container).handle(request(tmp_path))
    assert len(model.inputs) == 24
    assert {i.model_id for i in model.inputs} == {"fixed-answerer"}
    assert {i.prompt for i in model.inputs} == {"Continue the work."}
    assert all(i.context == "summary" for i in model.inputs)
    assert len(result.repeats) == 12
    assert all(r.replay.turns_run == 2 for r in result.repeats)
    assert all(
        r.action_score.items[0].outcome_at(2).value == "ACTED" for r in result.repeats
    )
    assert result.stability.unstable_rate == 0
    assert len(result.retained_cells) == 4


def test_experiment_unstable_flagged_matched_removal_from_real_proposals(
    tmp_path: Path,
) -> None:
    result = HandlerReplayExperiment(
        create_replay_container(
            model=ModelFixture(True),
            scratch_root=tmp_path / "scratch",
            input_roots=(tmp_path,),
        )
    ).handle(request(tmp_path))
    assert result.stability.unstable_rate == 0.25
    assert result.stability.exclusion_rate_by_arm == {"mod": 0.5, "stock_rerun": 0.5}
    assert result.retained_cells == [("b2", "mod"), ("b2", "stock_rerun")]
    assert "UNSTABLE rate 0.250" in result.stability.report


def test_unavailable_repeat_is_removed_together_with_its_partner(
    tmp_path: Path,
) -> None:
    class MissingModel(ModelFixture):
        def continue_turn(self, turn):
            if (
                turn.boundary_id == "b1"
                and turn.arm == "mod"
                and turn.repeat_index == 2
            ):
                raise TimeoutError("fixture model unavailable")
            return super().continue_turn(turn)

    result = HandlerReplayExperiment(
        create_replay_container(
            model=MissingModel(),
            scratch_root=tmp_path / "scratch",
            input_roots=(tmp_path,),
        )
    ).handle(request(tmp_path))
    assert result.retained_cells == [("b2", "mod"), ("b2", "stock_rerun")]
    assert result.stability.cells[1].status.value == "UNAVAILABLE"
    assert result.stability.exclusion_rate_by_arm == {"mod": 0.5, "stock_rerun": 0.5}
    assert any(
        r.error == "TimeoutError: fixture model unavailable" for r in result.repeats
    )


def test_same_canonical_models_are_reused_by_existing_nodes() -> None:
    from omnimarket.nodes.node_compaction_action_score_compute.models.model_action_score import (
        ModelCallClassPolicy as OldPolicy,
    )
    from omnimarket.nodes.node_compaction_ground_truth_compute.models.model_ground_truth import (
        ModelGroundTruthResult as OldTruth,
    )
    from omnimarket.nodes.node_context_compaction_manifest_compute.models.model_transcript_manifest import (
        ModelTranscriptCase as OldBoundary,
    )

    assert OldBoundary is ModelTranscriptCase
    assert OldTruth is ModelGroundTruthResult
    assert OldPolicy is ModelCallClassPolicy


def test_repeat_sets_use_cm2_item_keys_when_model_names_only_the_lane(
    tmp_path: Path,
) -> None:
    class NamedLane(ModelFixture):
        def continue_turn(self, turn):
            answer = super().continue_turn(turn)
            return answer.model_copy(
                update={
                    "calls": tuple(
                        c.model_copy(update={"item_key": ""}) for c in answer.calls
                    )
                }
            )

    req = request(tmp_path)
    contexts = []
    for ctx in req.contexts:
        item = ctx.ground_truth.items[0].model_copy(
            update={"key": "OPEN_CLAIM|claim:worker"}
        )
        contexts.append(
            ctx.model_copy(
                update={
                    "ground_truth": ctx.ground_truth.model_copy(
                        update={"items": (item,)}
                    )
                }
            )
        )
    req = req.model_copy(update={"contexts": contexts})
    result = HandlerReplayExperiment(
        create_replay_container(
            model=NamedLane(),
            scratch_root=tmp_path / "scratch",
            input_roots=(tmp_path,),
        )
    ).handle(req)
    assert result.repeats[0].replay.proposed_call_set == [
        "onex.lane.relaunch|OPEN_CLAIM|claim:worker"
    ]


def test_cm3_malformed_refusals_and_text_mentions_never_count_as_progress(
    tmp_path: Path,
) -> None:
    class Malformed:
        def continue_turn(self, turn):
            return ModelAssistantTurn(
                text="worker",
                calls=(ModelToolCall(tool="ledger_append", row="", item_key="worker"),),
            )

    req = request(tmp_path).model_copy(
        update={
            "policy": ModelCallClassPolicy(
                progress_classes={EnumOpenLoopKind.OPEN_CLAIM: ("ledger_append",)}
            )
        }
    )
    result = HandlerReplayExperiment(
        create_replay_container(
            model=Malformed(),
            scratch_root=tmp_path / "scratch",
            input_roots=(tmp_path,),
        )
    ).handle(req)
    assert all(
        r.action_score.items[0].outcome_at(2).value == "IDLE" for r in result.repeats
    )
    assert all(
        c.refusal_reason.value == "MALFORMED"
        for r in result.repeats
        for c in r.replay.calls
    )


def test_cm3_future_until_overlay_is_not_due(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from omnimarket.models.compaction_measurement.action import ModelOpenLoopItem

    req = request(tmp_path)
    req = req.model_copy(
        update={
            "contexts": [
                c.model_copy(
                    update={
                        "action_items": (
                            ModelOpenLoopItem(
                                key="worker",
                                kind=EnumOpenLoopKind.OPEN_CLAIM,
                                until=datetime(2026, 11, 3, tzinfo=UTC),
                            ),
                        )
                    }
                )
                for c in req.contexts
            ]
        }
    )
    result = HandlerReplayExperiment(
        create_replay_container(
            model=ModelFixture(),
            scratch_root=tmp_path / "scratch",
            input_roots=(tmp_path,),
        )
    ).handle(req)
    assert all(
        r.action_score.items[0].outcome_at(2).value == "NOT_DUE" for r in result.repeats
    )
