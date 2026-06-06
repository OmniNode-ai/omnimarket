from __future__ import annotations

from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelPrWatchOrchestratorRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(
        ...,
        description="GitHub repository in OWNER/REPO form.",
        examples=["OmniNode-ai/omnimarket"],
    )
    pr_number: int = Field(..., gt=0, description="Pull request number to watch.")
    correlation_id: UUID = Field(
        default_factory=uuid4,
        description="Runtime correlation ID for command/result/event tracing.",
    )
    poll_interval_seconds: float = Field(
        default=10.0,
        ge=0.0,
        le=600.0,
        description="Seconds to wait between gh pr checks polls.",
    )
    timeout_seconds: float | None = Field(
        default=None,
        ge=0.0,
        le=86400.0,
        description="Optional watch timeout override. Defaults to contract descriptor.timeout_ms.",
    )
    required_only: bool = Field(
        default=False,
        description="When true, pass --required to gh pr checks.",
    )

    @field_validator("repo")
    @classmethod
    def _repo_must_be_owner_repo(cls, value: str) -> str:
        repo = value.strip()
        if repo.count("/") != 1 or any(not part.strip() for part in repo.split("/")):
            raise ValueError("repo must be in OWNER/REPO form")
        return repo
