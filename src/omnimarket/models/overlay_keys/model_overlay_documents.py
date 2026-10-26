# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The documents of the six deployment overlays: ``repos``, ``lab_hosts``, ``hygiene_vocab``,
``remote_lane``, ``idle_slot_fill`` and ``throughput_tick``.

Each overlay is the YAML mapping ``<root>/<id>/overlay.yaml`` of its id's fields, read through
``load_structured_overlay``. Every field of a document is one overlay key, named
``<id>.<field>``; the package ships no value for any of them. Other overlays name a repository by
role (``plugin_source``, ``private_data``, ``deploy``, ``knowledge_base``) and the ``repos``
overlay resolves a role to a directory name.
"""

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

# A registry root as a probe script quotes it: no quote, backtick, space or ``$(``.
RegistryRoot = Annotated[
    str, StringConstraints(min_length=1, pattern=r"^[A-Za-z0-9_$/.~-]+$")
]
# One directory name beside or under the registry root.
DirName = Annotated[
    str, StringConstraints(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
]
# The name of an environment variable.
EnvName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]


def _relative_path(value: str) -> str:
    if value.startswith("/") or ".." in value.split("/"):
        raise ValueError("must be a path inside the clone: relative, no '..' segment")
    return value


# A path inside a clone: relative, no ``..`` segment.
RelPath = Annotated[
    str,
    StringConstraints(min_length=1, pattern=r"^[A-Za-z0-9_./-]+$"),
    AfterValidator(_relative_path),
]
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

RepoRole = Literal["plugin_source", "private_data", "deploy", "knowledge_base"]


class _ModelOverlayBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelLabHostClone(_ModelOverlayBase):
    """A registry clone a pool host must carry."""

    repo: NonEmptyStr
    ref: NonEmptyStr
    path: NonEmptyStr


class ModelLabHostRow(_ModelOverlayBase):
    """One pool host row of the lab host table."""

    name: NonEmptyStr
    target: NonEmptyStr
    local: bool = False
    codex: bool = False
    available: bool = True
    unavailable_reason: str | None = None
    contributor: bool = False
    push: bool = True
    cap: int | None = Field(default=None, ge=1)
    reserve: int | None = Field(default=None, ge=0)
    unit_cores: float | None = Field(default=None, gt=0)
    penalty: float = Field(default=0.0, ge=0)
    max_cpu_c: float | None = Field(default=None, gt=0)
    model_port: int | None = Field(default=None, ge=1, le=65535)
    cloud_consent: str | None = None
    tenant_id: str | None = None
    ledger_bus_lane: str | None = None
    ledger_bus_python: str | None = None
    parity_python: str | None = None
    parity_clones: tuple[NonEmptyStr, ...] = ()
    gh_absent: bool = False
    lab_work_root: str | None = None
    report_retention_days: int | None = Field(default=None, ge=1)
    clones: tuple[ModelLabHostClone, ...] = ()


class ModelLabHostsTable(_ModelOverlayBase):
    """The lab host table: the document a file named by ``ONEX_LAB_RUN_HOSTS`` holds."""

    hosts: Annotated[tuple[ModelLabHostRow, ...], Field(min_length=1)]
    # The declared bus lane the pool serves lab work on; carried by the table shape, not an overlay key.
    bus_lane: str | None = None


class ModelLabHostsOverlay(ModelLabHostsTable):
    """Keys ``lab_hosts.hosts`` and ``lab_hosts.registry_root_candidates``."""

    # Registry roots tried in order on a probed host; ``$HOME`` expands there.
    registry_root_candidates: Annotated[tuple[RegistryRoot, ...], Field(min_length=1)]


class ModelReposOverlay(_ModelOverlayBase):
    """Keys ``repos.*``: the directory name of each repository role, and the variables that hold a clone path."""

    plugin_source: DirName
    private_data: DirName
    knowledge_base: DirName
    deploy: DirName
    # Variable names in the order a reader tries them; the first one set names the private data clone.
    private_data_home_envs: Annotated[tuple[EnvName, ...], Field(min_length=1)]
    knowledge_base_home_env: EnvName
    # Roles whose clone sits beside the registry root, not under it.
    beside_root: Annotated[
        tuple[Literal["private_data", "knowledge_base"], ...], Field(min_length=1)
    ]


class ModelHygieneVocabOverlay(_ModelOverlayBase):
    """Keys ``hygiene_vocab.*``: two vocabulary files inside the private data clone."""

    vocabulary_rel: RelPath
    lab_vocabulary_rel: RelPath


class ModelRemoteLaneOverlay(_ModelOverlayBase):
    """Keys ``remote_lane.*``."""

    runtime_config_rel: RelPath
    # Roles whose repository a cloud lane never takes.
    cloud_excluded_roles: Annotated[tuple[RepoRole, ...], Field(min_length=1)]


class ModelCloudBacklog(_ModelOverlayBase):
    """What a free cloud slot may take from the backlog; changing a list is a new operator ruling."""

    ruling: NonEmptyStr
    note: NonEmptyStr
    repos: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    excluded_repos: tuple[NonEmptyStr, ...]
    lab_markers: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    sprint_state_types: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    max_estimate: int = Field(ge=0)
    window_hours: int = Field(ge=1)


class ModelExperimentDesign(_ModelOverlayBase):
    """One idle-slot experiment."""

    id: NonEmptyStr
    title: NonEmptyStr
    repo: NonEmptyStr
    design: NonEmptyStr
    measure: NonEmptyStr


class ModelIdleSlotFillOverlay(_ModelOverlayBase):
    """Keys ``idle_slot_fill.*``."""

    process_fix_roles: Annotated[tuple[RepoRole, ...], Field(min_length=1)]
    mvp_repos: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    always_out_roles: Annotated[tuple[RepoRole, ...], Field(min_length=1)]
    cloud_backlog: ModelCloudBacklog
    experiment_designs: Annotated[
        tuple[ModelExperimentDesign, ...], Field(min_length=1)
    ]


class ModelThroughputTickOverlay(_ModelOverlayBase):
    """Keys ``throughput_tick.*``: the fix texts of the host-tick checks."""

    host_tick_fix: NonEmptyStr
    host_tick_owned_fix: NonEmptyStr
    host_ticks_fix: NonEmptyStr
