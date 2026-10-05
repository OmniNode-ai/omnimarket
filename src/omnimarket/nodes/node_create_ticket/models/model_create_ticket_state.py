"""Models for node_create_ticket."""

from pydantic import BaseModel, ConfigDict


class ModelCreateTicketCompletedEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = "completed"
    correlation_id: str = ""
    ticket_id: str = ""
    ticket_url: str = ""
    contract_completeness: str = ""
