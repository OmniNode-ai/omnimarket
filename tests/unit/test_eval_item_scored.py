# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The record kept for every scored try, and its full trace (OMN-24170).

AC1: the scored-item model refuses the five malformed shapes and accepts the
baseline label. Each refusal test breaks exactly one rule, so deleting that rule
from the model turns exactly that test red (one mutation run per rule).
AC2: ``event_id`` is a uuid5 of the eight identity fields, and a code row
written before the new fields still validates.
AC3: a trace is never cut: it splits into parts that reassemble byte for byte
to its sha256, and a part that was altered is refused.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5

import pytest
from pydantic import ValidationError

from omnimarket.enums.enum_eval_scorer import EnumEvalScorer
from omnimarket.enums.enum_eval_unscorable_reason import (
    EnumEvalUnscorableClass,
    EnumEvalUnscorableReason,
)
from omnimarket.models.context_roi_trial_fields import ModelTrialFields
from omnimarket.models.eval_item.model_eval_item_scored import (
    CONTEXT_ARM_UNKNOWN,
    ModelEvalItemScored,
)
from omnimarket.models.eval_item.model_eval_item_traced import (
    DEFAULT_TRACE_BOUND,
    ModelEvalCheckTrace,
    ModelEvalItemTrace,
    ModelEvalItemTracePart,
    ModelEvalTraceBound,
    assemble_trace,
    split_trace,
)
from omnimarket.models.model_factor_arm import EnumArmLabel
from omnimarket.models.model_factor_matrix import build_canonical_factor_matrix

SCORED_AT = datetime(2026, 11, 14, 4, 0, tzinfo=UTC)
THREE_MB = 3 * 1024 * 1024


def _item(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "tenant_id": "dev",
        "run_id": "run-1",
        "task_set_id": "suite.v1",
        "task_set_digest": "a" * 64,
        "task_id": "task-1",
        "task_type": "code",
        "size_band": "S",
        "engine": "local",
        "model_id": "qwen",
        "model_revision": "sha256:" + "b" * 64,
        "generation_settings_hash": "c" * 64,
        "prompt_template_version": "v1",
        "context_arm": "off",
        "scorer": "contract_checks",
        "scorer_version": "1",
        "passed": True,
        "scored_at": SCORED_AT,
        "try_ordinal": 1,
        "replicate": 0,
    }
    base.update(changes)
    return base


# --- AC1: the five refusals and the baseline -------------------------------


def test_refuses_unscorable_without_reason() -> None:
    with pytest.raises(ValidationError, match="unscorable_reason"):
        ModelEvalItemScored(**_item(passed=None))


def test_accepts_unscorable_with_reason() -> None:
    row = ModelEvalItemScored(
        **_item(passed=None, unscorable_reason="revision_unobserved")
    )
    assert row.passed is None
    assert row.unscorable_reason is EnumEvalUnscorableReason.REVISION_UNOBSERVED
    assert row.unscorable_reason.reason_class is EnumEvalUnscorableClass.INFRASTRUCTURE


def test_refuses_context_label_outside_the_vocabulary() -> None:
    with pytest.raises(ValidationError, match="context_arm"):
        ModelEvalItemScored(**_item(context_arm="not_an_arm"))


def test_refuses_try_ordinal_below_one() -> None:
    with pytest.raises(ValidationError, match="try_ordinal"):
        ModelEvalItemScored(**_item(try_ordinal=0))


@pytest.mark.parametrize("replicate", [-1, 2])
def test_refuses_replicate_outside_zero_and_one(replicate: int) -> None:
    with pytest.raises(ValidationError, match="replicate"):
        ModelEvalItemScored(**_item(replicate=replicate))


@pytest.mark.parametrize("scorer", ["contract_checks", "answer_fields"])
def test_refuses_unknown_label_with_any_scorer_but_the_reviewer_model(
    scorer: str,
) -> None:
    with pytest.raises(ValidationError, match="unknown"):
        ModelEvalItemScored(**_item(context_arm=CONTEXT_ARM_UNKNOWN, scorer=scorer))


def test_accepts_unknown_label_with_the_reviewer_model() -> None:
    row = ModelEvalItemScored(
        **_item(context_arm=CONTEXT_ARM_UNKNOWN, scorer="reviewer_model")
    )
    assert row.context_arm == "unknown"


def test_accepts_the_baseline_label() -> None:
    row = ModelEvalItemScored(**_item(context_arm="off"))
    assert row.context_arm == "off"
    assert row.scorer is EnumEvalScorer.CONTRACT_CHECKS
    baseline = [a for a in build_canonical_factor_matrix() if not a.factors]
    assert [a.label.value for a in baseline] == ["off"]


@pytest.mark.parametrize("label", [a.value for a in EnumArmLabel])
def test_every_arm_label_of_the_factor_matrix_is_in_the_vocabulary(label: str) -> None:
    assert ModelEvalItemScored(**_item(context_arm=label)).context_arm == label


# --- AC2: the event id and the old row --------------------------------------

IDENTITY_CHANGES: dict[str, Any] = {
    "scorer": "answer_fields",
    "run_id": "run-2",
    "task_id": "task-2",
    "engine": "glm",
    "model_id": "other-model",
    "context_arm": "golden_only",
    "replicate": 1,
    "try_ordinal": 2,
}


def test_event_id_is_equal_across_two_constructions() -> None:
    first = ModelEvalItemScored(**_item())
    second = ModelEvalItemScored(**_item())
    assert first.event_id == second.event_id
    assert isinstance(first.event_id, UUID)
    assert first.event_id.version == 5


@pytest.mark.parametrize("field", sorted(IDENTITY_CHANGES))
def test_event_id_changes_when_any_one_identity_field_changes(field: str) -> None:
    base = ModelEvalItemScored(**_item())
    changed = ModelEvalItemScored(**_item(**{field: IDENTITY_CHANGES[field]}))
    assert changed.event_id != base.event_id


def test_event_id_ignores_fields_outside_the_identity() -> None:
    base = ModelEvalItemScored(**_item())
    other = ModelEvalItemScored(**_item(passed=False, scorer_version="2", host="h1"))
    assert other.event_id == base.event_id


def test_event_id_does_not_collide_across_a_field_boundary() -> None:
    one = ModelEvalItemScored(**_item(run_id="a|b", task_id="c"))
    two = ModelEvalItemScored(**_item(run_id="a", task_id="b|c"))
    assert one.event_id != two.event_id


def test_a_supplied_event_id_that_is_not_the_identity_is_refused() -> None:
    good = ModelEvalItemScored(**_item())
    assert (
        ModelEvalItemScored(**_item(event_id=good.event_id)).event_id == good.event_id
    )
    with pytest.raises(ValidationError, match="event_id"):
        ModelEvalItemScored(**_item(event_id=uuid5(good.event_id, "other")))


def test_a_dumped_row_validates_again() -> None:
    row = ModelEvalItemScored(**_item())
    assert ModelEvalItemScored.model_validate_json(row.model_dump_json()) == row


def test_scoring_id_follows_event_id_scored_at_and_trace_digest() -> None:
    base = ModelEvalItemScored(**_item(trace_sha256="d" * 64))
    same = ModelEvalItemScored(**_item(trace_sha256="d" * 64))
    later = ModelEvalItemScored(
        **_item(trace_sha256="d" * 64, scored_at=datetime(2026, 11, 14, 5, tzinfo=UTC))
    )
    other_trace = ModelEvalItemScored(**_item(trace_sha256="e" * 64))
    assert base.scoring_id == same.scoring_id
    assert later.event_id == base.event_id
    assert later.scoring_id != base.scoring_id
    assert other_trace.scoring_id != base.scoring_id


def test_a_code_row_written_before_the_new_fields_still_validates() -> None:
    old_row = {
        "check_verdict": None,
        "row_kind": "try",
        "suite_digest": "f" * 64,
        "size_band": "M",
        "try_ordinal": 2,
        "diff_hash": "9" * 64,
    }
    fields = ModelTrialFields.model_validate(old_row)
    assert fields.experiment_id == ""
    assert fields.replicate == 0
    assert fields.model_revision == ""
    assert fields.scorer_version == ""
    assert fields.generation_settings_hash == ""
    assert fields.prompt_template_version == ""


def test_the_code_row_takes_the_new_fields() -> None:
    fields = ModelTrialFields.model_validate(
        {
            "experiment_id": "exp-1",
            "replicate": 1,
            "model_revision": "sha256:abc",
            "scorer_version": "3",
            "generation_settings_hash": "1" * 64,
            "prompt_template_version": "p2",
        }
    )
    assert fields.replicate == 1
    assert fields.model_revision == "sha256:abc"
    with pytest.raises(ValidationError, match="replicate"):
        ModelTrialFields.model_validate({"replicate": 2})


# --- AC3: a trace is never cut ----------------------------------------------


def _trace(output_bytes: int = 64, **changes: Any) -> ModelEvalItemTrace:
    fields: dict[str, Any] = {
        "input_text": "write the function",
        "generation_settings": (("temperature", "0"), ("max_tokens", "4096")),
        "response_text": "def f():\n    return 1\n",
        "diff_text": "--- a\n+++ b\n@@\n+x\n",
        "checks": (
            ModelEvalCheckTrace(
                command="pytest -q",
                exit_code=1,
                output="x" * output_bytes,
            ),
        ),
    }
    fields.update(changes)
    return ModelEvalItemTrace(**fields)


def test_a_three_megabyte_output_round_trips_through_its_parts() -> None:
    output = ("check output line é✓\n" * (THREE_MB // 20))[:THREE_MB]
    trace = _trace(output_bytes=0)
    trace = trace.model_copy(
        update={
            "checks": (ModelEvalCheckTrace(command="c", exit_code=1, output=output),)
        }
    )
    event_id = uuid5(UUID(int=1), "event")
    scoring_id = uuid5(UUID(int=1), "scoring")
    parts = split_trace(trace, event_id=event_id, scoring_id=scoring_id)
    assert len(parts) > 1
    assert all(p.part_count == len(parts) for p in parts)
    assert [p.part_index for p in parts] == list(range(len(parts)))
    for part in parts:
        raw = base64.b64decode(part.chunk_b64)
        assert (
            len(part.model_dump_json().encode())
            <= DEFAULT_TRACE_BOUND.broker_max_request_size
        )
        assert len(raw) <= DEFAULT_TRACE_BOUND.max_part_bytes
    rebuilt = assemble_trace(tuple(reversed(parts)))
    assert rebuilt == trace
    assert rebuilt.checks[0].output == output
    assert rebuilt.sha256() == parts[0].trace_sha256 == trace.sha256()
    assert hashlib.sha256(trace.canonical_bytes()).hexdigest() == trace.sha256()


def test_a_trace_under_the_bound_is_one_part() -> None:
    parts = split_trace(
        _trace(), event_id=uuid5(UUID(int=1), "e"), scoring_id=uuid5(UUID(int=1), "s")
    )
    assert len(parts) == 1
    assert assemble_trace(parts) == _trace()


def test_parts_split_inside_a_multibyte_character_still_reassemble() -> None:
    trace = _trace(response_text="é✓😀" * 50)
    bound = ModelEvalTraceBound(
        broker_max_request_size=4096, envelope_headroom_bytes=2048, max_part_bytes=7
    )
    parts = split_trace(
        trace,
        event_id=uuid5(UUID(int=1), "e"),
        scoring_id=uuid5(UUID(int=1), "s"),
        bound=bound,
    )
    assert len(parts) > 10
    assert assemble_trace(parts) == trace


def _split_small() -> tuple[ModelEvalItemTracePart, ...]:
    bound = ModelEvalTraceBound(
        broker_max_request_size=4096, envelope_headroom_bytes=2048, max_part_bytes=64
    )
    return split_trace(
        _trace(output_bytes=500),
        event_id=uuid5(UUID(int=1), "e"),
        scoring_id=uuid5(UUID(int=1), "s"),
        bound=bound,
    )


def _alter_one_byte(part: ModelEvalItemTracePart) -> dict[str, Any]:
    raw = bytearray(base64.b64decode(part.chunk_b64))
    raw[0] ^= 0x01
    data = part.model_dump()
    data["chunk_b64"] = base64.b64encode(bytes(raw)).decode()
    return data


def test_a_part_with_one_byte_altered_is_refused_on_its_own() -> None:
    part = _split_small()[1]
    with pytest.raises(ValidationError, match="chunk_sha256"):
        ModelEvalItemTracePart.model_validate(_alter_one_byte(part))


def test_a_part_altered_together_with_its_own_digest_is_refused_on_assembly() -> None:
    parts = list(_split_small())
    data = _alter_one_byte(parts[1])
    data["chunk_sha256"] = hashlib.sha256(
        base64.b64decode(data["chunk_b64"])
    ).hexdigest()
    parts[1] = ModelEvalItemTracePart.model_validate(data)
    with pytest.raises(ValueError, match="sha256"):
        assemble_trace(tuple(parts))


def test_a_missing_part_is_refused() -> None:
    parts = _split_small()
    assert len(parts) > 2
    with pytest.raises(ValueError, match="missing"):
        assemble_trace(parts[:1] + parts[2:])


def test_a_duplicated_part_is_refused() -> None:
    parts = _split_small()
    with pytest.raises(ValueError, match="duplicate"):
        assemble_trace((*parts, parts[0]))


def test_parts_of_two_scorings_are_not_mixed() -> None:
    parts = list(_split_small())
    other = parts[1].model_copy(update={"scoring_id": uuid5(UUID(int=1), "other")})
    parts[1] = other
    with pytest.raises(ValueError, match="scoring_id"):
        assemble_trace(tuple(parts))


def test_no_parts_is_refused() -> None:
    with pytest.raises(ValueError, match="no parts"):
        assemble_trace(())


def test_the_bound_must_fit_under_the_broker_after_base64() -> None:
    with pytest.raises(ValidationError, match="exceeds"):
        ModelEvalTraceBound(
            broker_max_request_size=1000,
            envelope_headroom_bytes=100,
            max_part_bytes=900,
        )
    assert DEFAULT_TRACE_BOUND.max_part_bytes > 0
