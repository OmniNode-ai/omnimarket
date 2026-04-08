"""Models for node_design_to_plan."""

from pydantic import BaseModel, ConfigDict


class ModelDesignToPlanStartCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = ""
    phase: str = "brainstorm"
    topic: str = ""
    plan_path: str = ""
    no_launch: bool = False


class ModelDesignToPlanCompletedEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = "completed"
    correlation_id: str = ""
    plan_path: str = ""
    phase_reached: str = ""
