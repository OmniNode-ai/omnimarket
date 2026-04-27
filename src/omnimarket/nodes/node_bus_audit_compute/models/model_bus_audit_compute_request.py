from pydantic import BaseModel, ConfigDict, Field


class ModelBusAuditComputeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str = Field(default="", description="Scope of the bus audit.")
