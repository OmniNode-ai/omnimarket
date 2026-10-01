"""OMN-20242 content-free disposition wire contract."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from omnimarket.enums.enum_delegation_disposition import (
    DISPOSITION_REASONS,
    EnumDelegationDisposition,
    EnumDelegationDispositionReason,
)
from omnimarket.events.delegation_disposition import (
    ModelDelegationDispositionRecorded,
    disposition_id_for,
)

pytestmark = pytest.mark.unit
T0 = datetime(2026, 9, 30, tzinfo=UTC)
CORRELATION = UUID("22222222-2222-2222-2222-222222222222")


def event(**updates: Any) -> ModelDelegationDispositionRecorded:
    data = {
        "tenant_id": UUID("11111111-1111-1111-1111-111111111111"),
        "delegation_correlation_id": CORRELATION,
        "caller_lane": "deleg-omn-20242",
        "disposition": "accepted_as_is",
        "reason_code": "correct_as_is",
        "engine": "harness:claude-glm",
        "artifact_kind": "pull_request",
        "artifact_ref": "OmniNode-ai/omnimarket#42",
        "edit_ratio": None,
        "ticket_id": "OMN-20242",
        "answer_sha256": "a" * 64,
        "recorded_at": T0,
    }
    data.update(updates)
    return ModelDelegationDispositionRecorded.build(**data)


@pytest.mark.parametrize("disposition", list(EnumDelegationDisposition))
def test_each_disposition_builds_and_json_round_trips(
    disposition: EnumDelegationDisposition,
) -> None:
    value = event(
        disposition=disposition,
        reason_code=next(iter(DISPOSITION_REASONS[disposition])),
        edit_ratio=0.25 if disposition == "edited" else None,
    )
    assert (
        ModelDelegationDispositionRecorded.model_validate(value.model_dump(mode="json"))
        == value
    )
    with pytest.raises(ValidationError):
        value.caller_lane = "other"


@pytest.mark.parametrize(
    "updates",
    [
        {"reason_code": "wrong_answer"},
        {"disposition": "unknown"},
        {"reason_code": "unknown"},
        {"edit_ratio": 0.1},
        {"disposition": "edited", "reason_code": "minor_fix"},
        {"disposition": "edited", "reason_code": "minor_fix", "edit_ratio": -0.1},
        {"disposition": "edited", "reason_code": "minor_fix", "edit_ratio": 1.1},
        {
            "disposition": "edited",
            "reason_code": "minor_fix",
            "edit_ratio": float("nan"),
        },
        {"disposition": "rejected", "reason_code": "wrong_answer", "edit_ratio": 0},
        {"disposition": "ignored", "reason_code": "not_needed", "edit_ratio": 0},
        {"artifact_kind": "none"},
        {"artifact_ref": None},
        {"artifact_kind": "none", "artifact_ref": None},
        {
            "disposition": "edited",
            "reason_code": "minor_fix",
            "edit_ratio": 0.5,
            "artifact_kind": "none",
            "artifact_ref": None,
        },
        {"artifact_ref": "owner/repo#0"},
        {"artifact_ref": "owner/repo#01"},
        {"artifact_ref": "owner/repo#1\n"},
        {"artifact_ref": "https://github.com/o/r/pull/1"},
        {"artifact_kind": "commit", "artifact_ref": "ABCDEF0"},
        {"artifact_kind": "commit", "artifact_ref": "abc"},
        {"artifact_kind": "commit", "artifact_ref": "a" * 41},
        {"artifact_kind": "document", "artifact_ref": " docs/a.md"},
        {"artifact_kind": "document", "artifact_ref": "docs/a.md "},
        {"artifact_kind": "document", "artifact_ref": "docs/a\nb.md"},
        {"artifact_kind": "document", "artifact_ref": "docs/a\rb.md"},
        {"artifact_kind": "document", "artifact_ref": "../docs/a.md"},
        {"artifact_kind": "document", "artifact_ref": "docs/../a.md"},
        {"artifact_kind": "document", "artifact_ref": "docs/.."},
        {"artifact_kind": "document", "artifact_ref": ""},
        {"artifact_kind": "document", "artifact_ref": "a" * 513},
        {"caller_lane": "bad lane"},
        {"caller_lane": "a|b"},
        {"caller_lane": "a" * 129},
        {"caller_lane": ""},
        {"caller_lane": "lane\n"},
        {"engine": "GLM"},
        {"engine": "bad engine"},
        {"engine": ""},
        {"engine": "a" * 65},
        {"engine": "lab\n"},
        {"ticket_id": "omn-20242"},
        {"ticket_id": "OMN-0"},
        {"ticket_id": "OMN-1\n"},
        {"answer_sha256": "A" * 64},
        {"answer_sha256": "a" * 63},
        {"answer_sha256": "g" * 64},
        {"answer_sha256": "a" * 64 + "\n"},
        {"recorded_at": datetime(2026, 9, 30)},
        {"tenant_id": "bad"},
        {"delegation_correlation_id": "bad"},
    ],
)
def test_refuses_invalid_input(updates: dict[str, Any]) -> None:
    with pytest.raises((ValidationError, ValueError)):
        event(**updates)


@pytest.mark.parametrize("disposition", list(EnumDelegationDisposition))
@pytest.mark.parametrize("reason", list(EnumDelegationDispositionReason))
def test_reason_grouping(
    disposition: EnumDelegationDisposition, reason: EnumDelegationDispositionReason
) -> None:
    kwargs = {
        "disposition": disposition,
        "reason_code": reason,
        "edit_ratio": 0.0 if disposition == "edited" else None,
    }
    if reason in DISPOSITION_REASONS[disposition]:
        assert event(**kwargs).reason_code == reason
    else:
        with pytest.raises(ValidationError):
            event(**kwargs)


def test_reason_groups_are_exact() -> None:
    expected = {
        "accepted_as_is": {"correct_as_is", "verified_against_source"},
        "edited": {"minor_fix", "partial_use", "reformatted"},
        "rejected": {
            "wrong_answer",
            "hallucinated",
            "off_task",
            "incomplete",
            "unusable_format",
        },
        "ignored": {"not_needed", "superseded", "engine_failed", "lane_decided_first"},
    }
    assert expected == DISPOSITION_REASONS
    assert all(
        isinstance(reasons, frozenset) for reasons in DISPOSITION_REASONS.values()
    )


def test_id_is_deterministic_and_bound_to_event() -> None:
    first = event()
    assert first.disposition_id == disposition_id_for(
        CORRELATION, first.caller_lane, T0
    )
    assert first == event()
    assert (
        first.disposition_id
        != event(recorded_at=T0 + timedelta(microseconds=1)).disposition_id
    )
    assert (
        first.disposition_id
        == event(recorded_at=T0.astimezone(timezone(timedelta(hours=3)))).disposition_id
    )
    for updates in (
        {"disposition_id": uuid4()},
        {"caller_lane": "other"},
        {"delegation_correlation_id": uuid4()},
        {"recorded_at": T0 + timedelta(seconds=1)},
    ):
        with pytest.raises(ValidationError):
            ModelDelegationDispositionRecorded.model_validate(
                first.model_dump() | updates
            )


def test_requires_producer_recorded_at_and_id() -> None:
    for field in ("recorded_at", "disposition_id"):
        data = event().model_dump()
        data.pop(field)
        with pytest.raises(ValidationError):
            ModelDelegationDispositionRecorded.model_validate(data)


def test_field_set_carries_no_content() -> None:
    assert (
        not {"prompt", "prompt_text", "response", "response_text", "answer", "content"}
        & ModelDelegationDispositionRecorded.model_fields.keys()
    )


@pytest.mark.parametrize(
    "field", ["prompt", "prompt_text", "response", "response_text", "answer", "content"]
)
def test_extra_content_fields_are_refused_on_the_wire(field: str) -> None:
    with pytest.raises(ValidationError):
        ModelDelegationDispositionRecorded.model_validate(
            event().model_dump(mode="json") | {field: "secret"}
        )


@pytest.mark.parametrize(
    ("kind", "ref"),
    [
        ("commit", "abcdef0"),
        ("commit", "a" * 40),
        ("document", "docs/a.md"),
        ("document", "notes.v1.md"),
    ],
)
def test_valid_artifact_refs(kind: str, ref: str) -> None:
    assert event(artifact_kind=kind, artifact_ref=ref).artifact_ref == ref


@pytest.mark.parametrize(
    ("disposition", "reason"), [("rejected", "wrong_answer"), ("ignored", "not_needed")]
)
def test_unusable_answers_need_no_artifact_or_engine(
    disposition: str, reason: str
) -> None:
    value = event(
        disposition=disposition,
        reason_code=reason,
        artifact_kind="none",
        artifact_ref=None,
        engine=None,
        ticket_id=None,
        answer_sha256=None,
    )
    assert value.artifact_ref is None
