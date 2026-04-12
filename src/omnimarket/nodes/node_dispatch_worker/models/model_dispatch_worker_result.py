"""ModelDispatchWorkerResult — output of worker dispatch compilation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelDispatchWorkerResult(BaseModel):
    """Compiled worker dispatch ready for skill-layer execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    validated_task_description: str
    validated_prompt_template: str
    proposed_agent_spawn_args: dict[str, str]
    collision_fence_embeds: list[str]
    rejected_reason: str = ""


__all__: list[str] = ["ModelDispatchWorkerResult"]
