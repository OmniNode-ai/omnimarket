# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B producer of the acceptance judge command: one completed delegation, one render request.

The input is the delegate-skill terminal as it is published, which is the response plus the
score keys and the publish-time envelope keys (``actual_score``, ``required_bar``,
``causation_id``, ``emitted_at``, ``schema_version``, ``entity_id``). That is the decode the
platform's other consumers of this topic use, ``ModelDelegateSkillTerminalProjection``. The
producer's strict response class refuses those keys, so it is the wrong decode for a consumer:
with it, every in-process terminal was dead-lettered at the decode boundary.

The rubric text is read once at composition and held; ``handle`` reads no file and calls no
model. The item id is the delegation's correlation id, opaque to the judge, and the serving
model is carried for the score cells but never shown to a judge.
"""

from __future__ import annotations

from importlib.resources import files

from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
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
        self, completed: ModelDelegateSkillTerminalProjection
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
