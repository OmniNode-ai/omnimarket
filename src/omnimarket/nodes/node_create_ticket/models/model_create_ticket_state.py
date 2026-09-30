"""Models for node_create_ticket."""

from pydantic import BaseModel, ConfigDict, field_validator


class ModelCreateTicketStartCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = ""
    title: str = ""
    from_contract: str = ""
    from_plan: str = ""
    milestone: str = ""
    repo: str = ""
    parent: str = ""
    blocked_by: str = ""
    project: str = ""
    team: str = "Omninode"
    pillar: str = ""
    allow_arch_violation: bool = False

    @field_validator("project")
    @classmethod
    def _refuse_project(cls, value: str) -> str:
        """Operator ruling 2026-09-30T14:30:05Z (OMN-17427): a new ticket is
        created in the Backlog with NO project. Fail loud, never drop it."""
        if value.strip():
            raise ValueError(
                f"project={value!r} refused: every new ticket is created in the "
                "Backlog with no project (operator ruling 2026-09-30T14:30:05Z, "
                "OMN-17427). Moving a ticket into a sprint is the operator's call."
            )
        return value


class ModelCreateTicketCompletedEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = "completed"
    correlation_id: str = ""
    ticket_id: str = ""
    ticket_url: str = ""
    contract_completeness: str = ""
