# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, result and overlay of the landing decision venv effect (OMN-24355).

Every setting the node uses (the rows from a pin key to a distribution, the interpreter arguments and the timeouts)
is a field of the overlay model; the handler holds none of them.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmpty = Annotated[str, StringConstraints(min_length=1)]


class ModelLandingDecisionVenvRequest(BaseModel):
    """Bring the decision venv at ``venv_dir`` to the versions the pin at ``pin_path`` names."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    pin_path: str
    venv_dir: str
    base_python: str


class ModelLandingDecisionVenvResult(BaseModel):
    """The run's outcome; ``reason`` names why a request was refused and is empty otherwise.

    ``installed`` means the run installed at least one distribution, ``ready`` means every one already stood at its
    pinned version, ``refused`` means the run ended on a typed reason. ``versions`` maps each distribution to the
    version the venv holds after the run.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    outcome: Literal["installed", "ready", "refused"]
    versions: dict[str, str] = Field(default_factory=dict)
    reason: str = ""


class ModelLandingDecisionVenvOverlay(BaseModel):
    """The deployed settings: ``overlays/node_landing_decision_venv_effect/overlay.yaml`` of the overlays repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Pin key to distribution name, one row per dependency the pin names; a pin without a row's key is refused.
    distributions: dict[NonEmpty, NonEmpty] = Field(min_length=1)
    #: Arguments after the base interpreter that create a venv (the venv directory is appended).
    venv_args: list[NonEmpty] = Field(min_length=1)
    #: Arguments after the venv interpreter that install (the ``name==version`` requirements are appended).
    pip_args: list[NonEmpty] = Field(min_length=1)
    venv_timeout_seconds: float = Field(gt=0)
    pip_timeout_seconds: float = Field(gt=0)
    probe_timeout_seconds: float = Field(gt=0)


class ModelCommandOutcome(BaseModel):
    """One subprocess's end: its exit code and stdout, whether it timed out, and the OS error class if it never started."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    returncode: int
    stdout: str = ""
    timed_out: bool = False
    os_error: str = ""
