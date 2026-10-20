"""Local render input; no client credentials travel on this command."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict


class ModelLabSecretStoreOverrideRequest(BaseModel):
    """The ephemeral kustomization, the base ConfigMap it layers over (relative to the
    kustomization's directory) and the two non-secret addressing values."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kustomization: Path
    addr: str
    environment_slug: str
    base_configmap: Path
