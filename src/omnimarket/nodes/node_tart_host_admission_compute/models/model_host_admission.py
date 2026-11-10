# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed requests and results of a Tart host's admission for the clean-VM walk (OMN-23204).

Three pure operations share these models: the plan of the guest credential route, the grade
of that route's evidence, and the admission receipt. No model holds a credential value: a
credential is named by its overlay name and its secret reference, and its value is injected
by the runner on the delivery step's stdin at run time.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumDeliveryStepKind(StrEnum):
    RAMDISK_CREATE = "ramdisk_create"
    CREDENTIAL_DELIVER = "credential_deliver"
    CREDENTIAL_LENGTH = "credential_length"
    RAMDISK_PROOF = "ramdisk_proof"
    RAMDISK_DETACH = "ramdisk_detach"


class ModelCredentialRef(_FrozenModel):
    """One credential the guest must be able to read: named, never valued."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    # The key's name in the secret store (never a value); the runner resolves it at delivery.
    secret_ref: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    guest_file: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class ModelCredentialRouteSettings(_FrozenModel):
    """The route's settings, supplied by an overlay and never by a default."""

    credentials: tuple[ModelCredentialRef, ...] = Field(min_length=1)
    ramdisk_volume: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    # Where the RAM disk is mounted in the guest; an absolute path on the guest's writable root.
    ramdisk_mount_point: str = Field(pattern=r"^/[A-Za-z0-9_./-]+$")
    ramdisk_megabytes: int = Field(ge=1, le=512)
    tart_path: str = Field(pattern=r"^[A-Za-z0-9_./~-]+$")

    @field_validator("credentials")
    @classmethod
    def _names_are_unique(
        cls, value: tuple[ModelCredentialRef, ...]
    ) -> tuple[ModelCredentialRef, ...]:
        names = [c.name for c in value]
        files = [c.guest_file for c in value]
        if len(set(names)) != len(names) or len(set(files)) != len(files):
            raise ValueError("credential names and guest files must be unique")
        return value


class ModelCredentialPlanRequest(_FrozenModel):
    vm_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    settings: ModelCredentialRouteSettings


class ModelDeliveryStep(_FrozenModel):
    kind: EnumDeliveryStepKind
    argv: tuple[str, ...]
    # The credential whose value the runner writes to this step's stdin; None for a step with no stdin.
    stdin_credential: str | None = None


class ModelCredentialPlan(_FrozenModel):
    steps: tuple[ModelDeliveryStep, ...]
    guest_mount: str
    status: Literal["passed", "failed"] = "passed"


class ModelCredentialEvidence(_FrozenModel):
    """What the run measured for one credential; carries counts and flags, never the value."""

    name: str
    delivered_exit_code: int
    guest_length: int = Field(ge=0)
    expected_length: int = Field(ge=1)
    on_ramdisk: bool
    # Hits of the credential's value in the stopped VM's disk image, and of a control value that
    # was deliberately written to the data volume (the scan must be able to see that one).
    disk_scan_hits: int = Field(ge=0)
    disk_scan_control_hits: int = Field(ge=0)
    value_in_walk_log: bool


class ModelCredentialGradeRequest(_FrozenModel):
    expected_names: tuple[str, ...] = Field(min_length=1)
    evidence: tuple[ModelCredentialEvidence, ...]


class ModelCredentialGrade(_FrozenModel):
    probe_credentials: Literal[
        "reachable_not_in_image", "not_reachable", "in_image", "not_proven"
    ]
    reasons: tuple[str, ...]
    status: Literal["passed", "failed"]


class ModelAdmissionRequest(_FrozenModel):
    """The nine probe results and the dates; a missing one is None and never admits."""

    host: str = Field(min_length=1)
    go_at: datetime
    probed_at: datetime
    tart_version: str | None = None
    image_digest: str | None = None
    guest_agent: str | None = None
    guest_capture: str | None = None
    lifecycle_observer: str | None = None
    lifecycle_controls: str | None = None
    probe_credentials: str | None = None
    vm_deleted: bool | None = None

    @field_validator("go_at", "probed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("a time must carry a timezone")
        return value


class ModelAdmissionReceipt(_FrozenModel):
    host: str
    admitted: bool
    # Fields the request left out, and fields present with a value that does not admit.
    missing: tuple[str, ...]
    failed: tuple[str, ...]
    role: Literal["tart"] | None
    probed_at: datetime
    status: Literal["passed", "failed"]
