from pydantic import BaseModel, ConfigDict, Field


class ModelBusAuditComputeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(default="", description="Audit result status.")
