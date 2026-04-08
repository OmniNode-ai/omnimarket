"""Models for node_redeploy."""

from pydantic import BaseModel, ConfigDict


class ModelRedeployStartCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = ""
    versions: str = ""
    skip_sync: bool = False
    skip_dockerfile_update: bool = False
    skip_infisical: bool = False
    verify_only: bool = False
    dry_run: bool = False
    resume: str = ""


class ModelRedeployCompletedEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = "completed"
    correlation_id: str = ""
    run_id: str = ""
