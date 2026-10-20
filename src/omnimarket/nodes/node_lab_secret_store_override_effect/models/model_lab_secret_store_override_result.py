"""Terminal result preserving the appender's exit codes and diagnostics."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelLabSecretStoreOverrideResult(BaseModel):
    """Successful edit or an explicit legacy refusal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: Literal[0, 1, 2]
    message: str
