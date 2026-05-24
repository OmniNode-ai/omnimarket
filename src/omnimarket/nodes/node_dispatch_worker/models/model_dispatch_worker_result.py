"""ModelDispatchWorkerResult — output of worker dispatch compilation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelDispatchWorkerResult(BaseModel):
    """Compiled worker dispatch ready for skill-layer execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    validated_task_description: str
    validated_prompt_template: str
    proposed_agent_spawn_args: dict[str, str]
    collision_fence_embeds: list[str]
    rejected_reason: str = ""

    # KB context evidence fields (populated when knowledge_context_level != "none")
    knowledge_context_bundle_hash: str = Field(
        default="",
        description="SHA-256[:16] of the injected KB context bundle. Empty when level='none'.",
    )
    bundle_level: str = Field(
        default="none",
        description="The knowledge_context_level that was applied (mirrors command input).",
    )
    source_backends_used: list[str] = Field(
        default_factory=list,
        description="KB backends that contributed context (e.g. ['repowise', 'wiki']).",
    )
    degraded_backends: list[str] = Field(
        default_factory=list,
        description="Backends that were requested but unavailable or timed out.",
    )
    injected_context_char_count: int = Field(
        default=0,
        description="Character count of the injected ## Knowledge Context section.",
    )


__all__: list[str] = ["ModelDispatchWorkerResult"]
