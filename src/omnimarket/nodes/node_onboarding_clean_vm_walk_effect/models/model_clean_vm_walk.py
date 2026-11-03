# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Immutable request, execution records and receipt for a clean VM walk."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EnumWalkStepKind(StrEnum):
    TART_CLONE = "tart_clone"
    TART_SET_SPEC = "tart_set_spec"
    TART_RUN = "tart_run"
    TART_IP = "tart_ip"
    GUEST_FETCH_SCRIPT = "guest_fetch_script"
    GUEST_VERIFY_CHECKSUM = "guest_verify_checksum"
    GUEST_RUN_SCRIPT = "guest_run_script"
    GUEST_READ_LOG = "guest_read_log"
    GUEST_DASHBOARD_START = "guest_dashboard_start"
    GUEST_DASHBOARD_PROBE_ROOT = "guest_dashboard_probe_root"
    GUEST_DASHBOARD_PROBE_PROJECTIONS = "guest_dashboard_probe_projections"
    GUEST_DASHBOARD_STOP = "guest_dashboard_stop"
    TART_STOP = "tart_stop"
    TART_DELETE = "tart_delete"


class EnumWalkOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


class EnumPhaseResult(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class StepNotDocumentedError(ValueError):
    """A command was refused before it could reach Tart."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelWalkRequest(_FrozenModel):
    """Pinned participant command and VM host; contract supplies VM defaults."""

    script_sha: str
    checksum_line: str
    providers: tuple[str, ...] = Field(min_length=1)
    local_url: str | None = None
    local_model: str | None = None
    ollama_model: str | None = None
    tart_host: str = Field(min_length=1)
    tart_path: str | None = Field(default=None, min_length=1)
    image: str | None = Field(default=None, min_length=1)
    cpu: int | None = Field(default=None, ge=1)
    memory_gb: int | None = Field(default=None, ge=1)
    vm_name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    step_timeout_seconds: int = Field(default=3600, ge=1)
    branches_installed: tuple[str, ...] = ()

    @field_validator("tart_host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if value.startswith("-") or re.search(r"[\s\x00-\x1f]", value):
            raise ValueError(
                "tart_host must be an SSH target, without options or whitespace"
            )
        return value

    @field_validator("script_sha")
    @classmethod
    def validate_sha(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-fA-F]{40}", value) is None:
            raise ValueError("script_sha must be exactly 40 hex characters")
        return value

    @field_validator("checksum_line")
    @classmethod
    def validate_checksum(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-fA-F]{64}  lab-onboarding\.sh", value) is None:
            raise ValueError(
                "checksum_line must be 64 hex characters, two spaces, lab-onboarding.sh"
            )
        return value

    @field_validator("providers")
    @classmethod
    def validate_providers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            p not in {"gemini", "openrouter", "openai", "ollama", "local"}
            for p in value
        ):
            raise ValueError("unsupported provider")
        return value

    @model_validator(mode="after")
    def validate_local(self) -> Self:
        if "local" in self.providers and (not self.local_url or not self.local_model):
            raise ValueError("local provider requires local_url and local_model")
        return self


class ModelRunnerResult(_FrozenModel):
    exit_code: int
    stdout: str
    stderr: str


class ModelWalkStep(_FrozenModel):
    kind: EnumWalkStepKind
    argv: tuple[str, ...]
    timeout_seconds: float = Field(gt=0)


class ModelWalkStepRecord(_FrozenModel):
    kind: EnumWalkStepKind
    argv: tuple[str, ...]
    exit_code: int
    seconds: float


class ModelPhaseRecord(_FrozenModel):
    phase_number: int
    name: str
    result: EnumPhaseResult
    seconds: float | None
    failure_text: str | None = None


class ModelDelegatedRun(_FrozenModel):
    backend: str | None = None
    status: Literal["success", "failed", "not_run"] = "not_run"
    run_id: str | None = None


class ModelDashboardResult(_FrozenModel):
    root_http_status: int | None = None
    projections_http_status: int | None = None
    base_url: str | None = None
    failure_text: str | None = None


class ModelVmSpec(_FrozenModel):
    image: str
    cpu: int
    memory_gb: int
    vm_name: str
    macos_version: str | None = None
    disk_gb: int | None = None


class ModelInstalledPin(_FrozenModel):
    name: str
    version: str
    source: str


class ModelWalkReceipt(_FrozenModel):
    outcome: EnumWalkOutcome
    script_sha: str
    checksum_line: str
    providers: tuple[str, ...]
    local_url: str | None
    vm_spec: ModelVmSpec
    phases: tuple[ModelPhaseRecord, ...]
    steps: tuple[ModelWalkStepRecord, ...]
    total_seconds: float
    seconds_to_first_delegated_run: float | None
    delegated_run: ModelDelegatedRun
    dashboard: ModelDashboardResult
    pins_installed: tuple[ModelInstalledPin, ...]
    omnibase_spi_version: str | None
    branches_installed: tuple[str, ...]
    vm_deleted: bool
    failure_text: str | None
    # The field the runtime's failure-terminal guard reads: a receipt carrying a
    # cause is published on the contract's declared failure terminal.
    terminal_failure_cause: str | None = None
    log_tail: str
    hand_fix_steps: tuple[str, ...]
    started_at: str
    finished_at: str
