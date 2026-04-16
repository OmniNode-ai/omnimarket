"""Task 3: Polish task taxonomy + envelope tests [OMN-8958]."""

from uuid import UUID

import pytest

from omnimarket.enums.enum_polish_task_class import EnumPolishTaskClass
from omnimarket.models.model_polish_task_envelope import ModelPolishTaskEnvelope


def test_enum_has_phase_1_and_phase_2_values() -> None:
    phase_1 = {
        EnumPolishTaskClass.AUTO_MERGE_ARM,
        EnumPolishTaskClass.REBASE,
        EnumPolishTaskClass.CI_RERUN,
    }
    phase_2 = {
        EnumPolishTaskClass.THREAD_REPLY,
        EnumPolishTaskClass.CONFLICT_HUNK,
        EnumPolishTaskClass.CI_FIX,
    }
    assert phase_1 | phase_2 == set(EnumPolishTaskClass)
    assert len(EnumPolishTaskClass) == 6


def test_envelope_declares_routing_policy_slot() -> None:
    env = ModelPolishTaskEnvelope(
        task_class=EnumPolishTaskClass.AUTO_MERGE_ARM,
        pr_number=100,
        repo="OmniNode-ai/omniclaude",
        correlation_id=UUID("00000000-0000-4000-a000-000000000001"),
        routing_policy=None,
    )
    assert env.routing_policy is None
    assert "routing_policy" in ModelPolishTaskEnvelope.model_fields


def test_envelope_is_frozen() -> None:
    env = ModelPolishTaskEnvelope(
        task_class=EnumPolishTaskClass.REBASE,
        pr_number=42,
        repo="OmniNode-ai/omnibase_core",
        correlation_id=UUID("00000000-0000-4000-a000-000000000002"),
    )
    with pytest.raises(Exception, match=r"frozen|immutable"):
        env.pr_number = 999  # type: ignore[misc]
