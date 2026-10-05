# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B producer of the acceptance judge command: one completed delegation, one render request.

The rubric text is read once at composition and held; ``handle`` reads no file and calls no
model. The item id is the delegation's correlation id, opaque to the judge, and the serving
model is carried for the score cells but never shown to a judge.
"""

from __future__ import annotations

from importlib.resources import files

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillCompleted,
)
from omnimarket.models.delegation_acceptance_judge.enum_acceptance_operation import (
    EnumAcceptanceOperation,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_item import (
    ModelAcceptanceItem,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_judge_request import (
    ModelAcceptanceJudgeRequest,
)

RUBRIC_RESOURCE = "configs/delegation_acceptance_judge_rubric.v1.yaml"


class HandlerDelegationAcceptanceJudgeRequest:
    """Turn a completed delegation into the judge's render request."""

    def __init__(self, rubric_yaml: str | None = None) -> None:
        self._rubric_yaml = (
            files("omnimarket").joinpath(RUBRIC_RESOURCE).read_text(encoding="utf-8")
            if rubric_yaml is None
            else rubric_yaml
        )

    def handle(
        self, completed: ModelDelegateSkillCompleted
    ) -> ModelAcceptanceJudgeRequest:
        item_id = str(completed.correlation_id)
        return ModelAcceptanceJudgeRequest(
            operation=EnumAcceptanceOperation.RENDER,
            rubric_yaml=self._rubric_yaml,
            seed=item_id,
            items=(
                ModelAcceptanceItem(
                    item_id=item_id,
                    model=completed.model_name or "unknown",
                    task_type=completed.task_type,
                    task_text=completed.prompt_text,
                    answer_text=completed.response,
                ),
            ),
        )
