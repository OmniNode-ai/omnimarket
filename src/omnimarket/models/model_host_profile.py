# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One declaration of a lab host, and everything derived from or measured against it."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

OMNI_HOME_TOKEN = "$OMNI_HOME"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelProfileClone(_Frozen):
    """One canonical clone the host carries."""

    repo: str = Field(description="owner/name of the repository")
    ref: str
    path: str = Field(
        description="absolute, or relative to the registry root via $OMNI_HOME/"
    )
    venv: bool = Field(default=False, description="the clone carries its own .venv")
    lab_run_row: bool = Field(
        default=True,
        description="the clone is listed in the pool host table's clones column",
    )
    reader: str = Field(
        default="",
        description="what reads this clone on the host; required when lab_run_row is false",
    )

    @model_validator(mode="after")
    def _reader_required(self) -> "ModelProfileClone":
        if not self.lab_run_row and not self.reader:
            raise ValueError(
                f"clone {self.repo}: a clone outside the pool table names its reader"
            )
        return self


class ModelProfilePrepushTable(_Frozen):
    """One pre-push host table the host has a row in."""

    file: str
    slots: int
    extra_columns: tuple[str, ...] = ()
    note: str = ""


class ModelProfilePrepush(_Frozen):
    role: str
    hostname: str
    ssh_target: str
    cores: int
    uv_abs_path: str
    uv_min_version: str
    workroot: str
    slot_mode: str
    repos_denied: str = "-"
    mode: str
    rows_target: str = Field(
        description="directory the host's derived rows are written to"
    )
    tables: tuple[ModelProfilePrepushTable, ...]


class ModelProfileEvidence(_Frozen):
    source: str
    locator: str
    completion_record: str = ""


class ModelProfileUnit(_Frozen):
    """One automatic process of the host; derives one liveness row."""

    name: str
    owner_repo: str
    trigger_kind: str
    native_id: str
    expected_interval_seconds: int
    max_runtime_seconds: int | None = None
    max_silence_seconds: int | None = None
    alarm_deadline_seconds: int
    run_record: str = "per-run"
    evidence: ModelProfileEvidence
    real_work: str
    state: str = "active"
    comment: str = ""


class ModelProfileLiveness(_Frozen):
    rows_target: str = Field(
        description="file the host's derived liveness rows are written to"
    )
    units: tuple[ModelProfileUnit, ...]


class ModelHostProfile(_Frozen):
    """A host declared once; clones, venvs, prepush and liveness rows derive from it."""

    host: str
    ssh_target: str | None = Field(
        default=None,
        description="ssh destination; None measures and acts on the local host",
    )
    registry_root: str = Field(description="the host's registry root, absolute")
    python: str = Field(
        description="interpreter the host's per-clone venvs are created with"
    )
    git_remote_template: str = Field(description="clone URL with a {repo} placeholder")
    unit_prefix: str = "onex-"
    systemd_units: tuple[str, ...] = Field(
        default=(),
        description="systemd units the host carries besides its liveness units",
    )
    clones: tuple[ModelProfileClone, ...]
    prepush: ModelProfilePrepush | None = None
    liveness: ModelProfileLiveness | None = None

    def resolve(self, path: str) -> str:
        """Resolve $OMNI_HOME in a declared path against this host's registry root."""
        return path.replace(OMNI_HOME_TOKEN, self.registry_root)


class ModelHostProfileDeriveRequest(_Frozen):
    profile: ModelHostProfile


class ModelDerivedClone(_Frozen):
    repo: str
    ref: str
    path: str


class ModelDerivedVenv(_Frozen):
    clone_path: str
    venv_path: str
    python: str


class ModelDerivedPrepushRow(_Frozen):
    file: str
    target: str = Field(
        description="resolved path the host's row for this table is written to"
    )
    line: str


class ModelDerivedLivenessRow(_Frozen):
    row: dict[str, object]


class ModelHostProfileDerived(_Frozen):
    host: str
    clone_rows: tuple[ModelDerivedClone, ...] = Field(
        description="the pool host table's clones column"
    )
    clones: tuple[ModelDerivedClone, ...] = Field(
        description="every declared clone, resolved"
    )
    venvs: tuple[ModelDerivedVenv, ...]
    prepush_rows: tuple[ModelDerivedPrepushRow, ...]
    prepush_workroot: str | None
    liveness_rows: tuple[dict[str, object], ...]
    liveness_rows_target: str | None
    liveness_rows_text: str
    liveness_native_ids: tuple[str, ...]
    systemd_units: tuple[str, ...]


class EnumDriftKind(StrEnum):
    MEASUREMENT_UNAVAILABLE = "measurement_unavailable"
    UNDECLARED_CLONE = "undeclared_clone"
    MISSING_CLONE = "missing_clone"
    MISSING_VENV = "missing_venv"
    MISSING_WORKROOT = "missing_workroot"
    MISSING_PREPUSH_ROW = "missing_prepush_row"
    MISSING_LIVENESS_ROW = "missing_liveness_row"
    MISSING_UNIT = "missing_unit"
    UNDECLARED_UNIT = "undeclared_unit"


class ModelHostMeasurement(_Frozen):
    """What a host actually carries, as read through the measurement port."""

    host: str
    errors: tuple[str, ...] = ()
    git_dirs_in_registry: tuple[str, ...] = Field(
        description="names of git checkouts directly under the registry root"
    )
    present_clone_paths: tuple[str, ...] = Field(
        description="resolved declared clone paths that hold a git checkout"
    )
    present_venvs: tuple[str, ...] = Field(
        description="resolved venv paths with an interpreter"
    )
    workroot_present: bool = False
    prepush_rows: dict[str, tuple[str, ...]] = Field(
        default_factory=dict, description="rows found per declared target file"
    )
    liveness_native_ids: tuple[str, ...] = Field(
        default=(), description="native ids found in the declared liveness rows target"
    )
    units: tuple[str, ...] = Field(description="systemd unit names the host carries")


class ModelDriftFinding(_Frozen):
    kind: EnumDriftKind
    subject: str


class ModelHostStateRequest(_Frozen):
    profile: ModelHostProfile


class ModelHostDriftCheckRequest(_Frozen):
    profile: ModelHostProfile


class ModelHostDriftCheckResult(_Frozen):
    host: str
    ok: bool
    findings: tuple[ModelDriftFinding, ...]
    measurement: ModelHostMeasurement


class EnumGapUnit(StrEnum):
    G1_CLONES = "g1_clones"
    G2_PREPUSH = "g2_prepush"
    G3_LIVENESS = "g3_liveness"
    G4_VENVS = "g4_venvs"


class EnumGapActionKind(StrEnum):
    CLONE = "clone"
    MKDIR = "mkdir"
    WRITE = "write"
    VENV = "venv"


class ModelGapAction(_Frozen):
    unit: EnumGapUnit
    kind: EnumGapActionKind
    path: str
    repo: str = ""
    ref: str = ""
    python: str = ""
    content: str = ""


class ModelHostGapUnitRequest(_Frozen):
    profile: ModelHostProfile
    unit: EnumGapUnit
    apply: bool = Field(
        default=False, description="false plans only; dry-run is the default"
    )


class ModelHostGapUnitResult(_Frozen):
    host: str
    unit: EnumGapUnit
    applied: bool
    planned: tuple[ModelGapAction, ...]
    done: tuple[ModelGapAction, ...]
