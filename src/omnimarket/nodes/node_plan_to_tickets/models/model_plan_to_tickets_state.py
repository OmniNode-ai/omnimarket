"""Models for node_plan_to_tickets."""

from pydantic import BaseModel, ConfigDict


class ModelPlanToTicketsStartCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = ""
    plan_path: str
    project: str = ""
    epic_title: str = ""
    no_create_epic: bool = False
    dry_run: bool = False
    skip_existing: bool = False
    team: str = "Omninode"
    repo: str = ""
    allow_arch_violation: bool = False


class ModelPlanToTicketsCompletedEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = "completed"
    correlation_id: str = ""
    created_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    epic_id: str = ""
