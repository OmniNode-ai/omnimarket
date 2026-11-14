# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the installer hands the decision environment node."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelLandingDecisionVenvRequest(BaseModel):
    """The decision pin, the environment directory and the interpreter that builds it.

    ``build`` creates the environment from scratch with the pinned libraries and then checks it;
    ``verify`` only checks an environment that already exists. ``runtime_file`` names the
    ``landing_runtime.json`` the controller reads; it is written only after the check passed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["build", "verify"]
    pin_file: Path
    venv_dir: Path
    base_python: Path | None = None
    node_src: Path | None = None
    runtime_file: Path | None = None
