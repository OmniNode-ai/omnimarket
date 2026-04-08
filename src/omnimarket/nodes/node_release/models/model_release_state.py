"""Models for node_release."""

from pydantic import BaseModel, ConfigDict


class ModelReleaseStartCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = ""
    repos: list[str] = []
    bump: str = ""
    dry_run: bool = False
    resume: str = ""
    skip_pypi_wait: bool = False
    autonomous: bool = False
    gate_attestation: str = ""


class ModelReleaseCompletedEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = "completed"
    correlation_id: str = ""
    run_id: str = ""
    repos_succeeded: int = 0
    repos_failed: int = 0
