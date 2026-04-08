"""Models for node_ticket_work."""

from pydantic import BaseModel, ConfigDict


class ModelTicketWorkStartCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = ""
    ticket_id: str
    autonomous: bool = False
    skip_to: str = ""


class ModelTicketWorkCompletedEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = "completed"
    correlation_id: str = ""
    ticket_id: str = ""
    extra_status: str = ""
    pr_url: str = ""
    phase_reached: str = ""
