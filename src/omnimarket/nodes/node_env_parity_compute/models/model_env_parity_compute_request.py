from pydantic import BaseModel, ConfigDict, Field


class ModelEnvParityComputeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str = Field(default="", description="Scope of the environment parity check.")
