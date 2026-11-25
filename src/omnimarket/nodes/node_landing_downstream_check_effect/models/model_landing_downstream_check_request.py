# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the landing controller hands the downstream check: the head, the repositories to try it in and the smoke."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_downstream_gate import SHA_PATTERN


class ModelDownstreamTarget(BaseModel):
    """One downstream repository: its name and a checkout of its default branch.

    ``test_args`` are the arguments of an extra command run by the venv's interpreter in the checkout (a pytest
    collection, for one), whose failure is a break; the smoke does not import test modules.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    checkout: Path
    test_args: tuple[str, ...] = ()


class ModelLandingDownstreamCheckRequest(BaseModel):
    """The head to try (``repo_url`` at ``head_sha``, installed as ``package``) and where to try it.

    ``smoke_script`` is the stdlib-only import smoke the repository's own CI runs
    (``tests/ci/latest_core_smoke.py``); it is run by path with each downstream venv's interpreter.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    package: str = Field(min_length=1)
    repo_url: str = Field(min_length=1)
    head_sha: str = Field(pattern=SHA_PATTERN)
    targets: tuple[ModelDownstreamTarget, ...] = Field(min_length=1)
    smoke_script: Path
    work_dir: Path
    installer: str = "uv"
    python_version: str = "3.12"
    venv_timeout_seconds: float = Field(default=120.0, gt=0)
    install_timeout_seconds: float = Field(default=900.0, gt=0)
    smoke_timeout_seconds: float = Field(default=600.0, gt=0)

    def core_spec(self) -> str:
        return f"{self.package} @ git+{self.repo_url}@{self.head_sha}"
