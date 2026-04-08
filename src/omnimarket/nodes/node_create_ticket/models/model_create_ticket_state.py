"""Models for node_create_ticket."""

from pydantic import BaseModel, ConfigDict


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
    allow_arch_violation: bool = False


class ModelCreateTicketCompletedEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = "completed"
    correlation_id: str = ""
    ticket_id: str = ""
    ticket_url: str = ""
    contract_completeness: str = ""
