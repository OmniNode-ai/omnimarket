# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared policy of the host retention node, and the report of one pass.

Every host, path and number the node acts on is a field of the overlay model below. The contract
declares only the bounds an overlay may not exceed (which leaf names a root may have, which
container prefixes are allowed) and the schedule.
"""

from __future__ import annotations

import os
import re
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


# A path an overlay writes as ``~/...`` or ``$OMNI_HOME/...`` is expanded where the pass runs, so no overlay names a home path;
# a variable that is not set stays as written, the path is then not absolute, and the pass refuses it.
HostPath = Annotated[
    str,
    BeforeValidator(
        lambda v: os.path.expandvars(os.path.expanduser(v)) if isinstance(v, str) else v
    ),
    StringConstraints(strip_whitespace=True, min_length=1),
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelAgeClass(_Frozen):
    """One directory whose direct entries age out after ``keep_days``.

    ``protected_name_patterns`` are full-match regular expressions of entry names that are never
    removed.
    """

    name: NonEmptyStr
    kind: Literal["lane_scratch", "lane_results", "pre_commit_store"]
    root: HostPath
    keep_days: float = Field(gt=0)
    protected_name_patterns: tuple[NonEmptyStr, ...] = ()
    index_db: HostPath | None = None

    @model_validator(mode="after")
    def patterns_compile(self) -> Self:
        for pattern in self.protected_name_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(
                    f"protected_name_patterns entry {pattern!r} is not a valid regex: {exc}"
                ) from exc
        return self

    @model_validator(mode="after")
    def index_db_rule(self) -> Self:
        if self.kind == "pre_commit_store":
            if self.index_db is None:
                raise ValueError("pre_commit_store requires index_db")
        elif self.index_db is not None:
            raise ValueError("index_db is only for pre_commit_store")
        return self


class ModelRunnerMount(_Frozen):
    """One runner mount: a host directory paired with a container path.

    ``disposable_patterns`` are fnmatch patterns on direct child names that may be evicted;
    ``keep_gib`` is the budget the mount holds at most.
    """

    host_root_template: HostPath
    container_root: (
        NonEmptyStr  # relative to the container user's home, e.g. `.cache/pip`
    )
    disposable_patterns: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    protected_names: tuple[NonEmptyStr, ...] = ()
    keep_gib: float = Field(ge=0)

    @model_validator(mode="after")
    def template_has_id(self) -> Self:
        if "{id}" not in self.host_root_template:
            raise ValueError("host_root_template must contain the literal {id}")
        return self


class ModelRunnerGroup(_Frozen):
    """A set of runner ids sharing one container template and mounts."""

    ids: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    container_template: NonEmptyStr
    container_user: NonEmptyStr  # the user the runner runs as: the removal runs as it, never as root
    mounts: Annotated[tuple[ModelRunnerMount, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def template_has_id(self) -> Self:
        if "{id}" not in self.container_template:
            raise ValueError("container_template must contain the literal {id}")
        return self


class ModelUvCacheCap(_Frozen):
    """The size cap of a uv cache.

    When the cache still holds more than ``cap_gib`` after the prune, the whole entries of each bucket
    (a path relative to the cache, whose depth the contract declares) are evicted oldest first until the
    cache is at or under the cap. An entry changed inside ``keep_days`` is never evicted.
    """

    cap_gib: float = Field(gt=0)
    buckets: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    keep_days: float = Field(gt=0)

    @model_validator(mode="after")
    def buckets_are_relative_paths(self) -> Self:
        for bucket in self.buckets:
            parts = bucket.split("/")
            if any(part in ("", ".", "..") for part in parts):
                raise ValueError(
                    f"bucket {bucket!r} must be a relative path of plain folder names"
                )
        if len(set(self.buckets)) != len(self.buckets):
            raise ValueError("a bucket is declared more than once")
        return self


class ModelUvArchiveEntry(_Frozen):
    """One whole entry of a uv cache bucket: ``name`` is its path relative to the bucket."""

    name: str
    bytes: int = Field(ge=0)
    newest_mtime: float
    linked: bool
    special: bool = False


class ModelUvCache(_Frozen):
    """One uv cache directory to prune when it exceeds ``min_gib``."""

    path: HostPath
    uv_binary: NonEmptyStr
    min_gib: float = Field(ge=0)
    ci_prune: bool = False
    timeout_seconds: float = Field(
        gt=0
    )  # `uv cache size` walks every file of the cache and can take minutes
    cap: ModelUvCacheCap | None = None

    @model_validator(mode="after")
    def cap_not_below_min(self) -> Self:
        if self.cap is not None and self.cap.cap_gib < self.min_gib:
            raise ValueError(
                f"cap_gib {self.cap.cap_gib} must be at least min_gib {self.min_gib}"
            )
        return self


class ModelHostRetention(_Frozen):
    """All deployment facts the node needs on one host."""

    name: NonEmptyStr
    docker_timeout_seconds: float = Field(
        gt=0
    )  # the most one docker call may take before the state reads as unknown
    lane_run_roots: tuple[HostPath, ...] = ()
    receipt_roots: tuple[HostPath, ...] = ()
    unit_roots: tuple[
        HostPath, ...
    ] = ()  # systemd unit directories: an entry a unit file names is never removed
    age_classes: tuple[ModelAgeClass, ...] = ()
    runner_groups: tuple[ModelRunnerGroup, ...] = ()
    uv_caches: tuple[ModelUvCache, ...] = ()
    docker_mount_guard: bool = (
        True  # keep any entry a running container bind-mounts (or holds a mount inside)
    )

    @model_validator(mode="after")
    def at_least_one_class(self) -> Self:
        if not self.age_classes and not self.runner_groups and not self.uv_caches:
            raise ValueError(
                "at least one of age_classes, runner_groups, uv_caches must be non-empty"
            )
        return self


class ModelHostRetentionOverlay(_Frozen):
    """The hosts that declare host retention."""

    hosts: Annotated[tuple[ModelHostRetention, ...], Field(min_length=1)]


class ModelRetentionBounds(_Frozen):
    """What an overlay may not exceed, owned by the contract."""

    min_keep_days: float = Field(gt=0)
    root_leaf_names: Annotated[
        dict[
            Literal[
                "lane_scratch",
                "lane_results",
                "runner_mount",
                "uv_cache",
                "pre_commit_store",
            ],
            tuple[NonEmptyStr, ...],
        ],
        Field(min_length=1),
    ]
    allowed_container_roots: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    protected_leaf_names_always: tuple[NonEmptyStr, ...] = ()
    uv_evictable_buckets: dict[NonEmptyStr, Annotated[int, Field(ge=1, le=3)]] = Field(
        default_factory=dict
    )  # bucket path relative to a uv cache -> depth of the whole entries under it


class ModelHostRetentionRequest(_Frozen):
    """Run one pass for ``host_name``.

    ``overlay_file`` pins the overlay document; when empty the node reads the environment
    variable and then the default overlay path. ``dry_run`` reads and reports and removes
    nothing. ``classes`` restricts which classes run; empty runs all.
    """

    host_name: NonEmptyStr
    overlay_file: str = ""
    dry_run: bool = False
    classes: tuple[
        Literal[
            "lane_scratch",
            "lane_results",
            "runner_work",
            "uv_cache",
            "pre_commit_store",
        ],
        ...,
    ] = ()


class ModelRetentionItem(_Frozen):
    """One entry the pass would remove or evict."""

    class_name: NonEmptyStr
    path: NonEmptyStr
    bytes: int = Field(default=0, ge=0)
    action: Literal["remove", "evict", "uv_prune"]
    reason: str = ""


class ModelRetentionKept(_Frozen):
    """One entry the pass kept, with the reason."""

    class_name: str
    path: str
    reason: Literal[
        "too_new",
        "held_by_process",
        "live_lane",
        "mounted_by_container",
        "referenced_by_unit",
        "runner_busy",
        "runner_state_unknown",
        "foreign_owner",
        "special_file",
        "linked_by_venv",
        "protected",
        "under_budget",
        "below_min",
        "store_locked",
    ]


class ModelDiskReading(_Frozen):
    """A disk usage reading for one path."""

    path: str
    used_bytes: int
    total_bytes: int
    used_percent: float


class ModelClassTotals(_Frozen):
    """Per-class totals for one pass."""

    class_name: str
    would_remove_count: int = 0
    would_remove_bytes: int = 0
    removed_count: int = 0
    freed_bytes: int = 0
    kept_count: int = 0


class ModelHostRetentionResult(_Frozen):
    """What one pass over one host's declared directories did.

    ``items`` lists entries removed or evicted; ``kept`` lists entries held with a reason;
    ``totals`` aggregates per class. ``status`` is ``refused`` when the pass was refused.
    """

    host: str
    dry_run: bool
    status: Literal["ok", "refused"]
    refusals: tuple[str, ...] = ()
    items: tuple[ModelRetentionItem, ...] = ()
    kept: tuple[ModelRetentionKept, ...] = ()
    totals: tuple[ModelClassTotals, ...] = ()
    total_bytes: int = 0
    disk_before: tuple[ModelDiskReading, ...] = ()
    disk_after: tuple[ModelDiskReading, ...] = ()
    errors: tuple[str, ...] = ()
    unreadable_process_count: int = 0

    @model_validator(mode="after")
    def status_consistency(self) -> Self:
        if self.status == "refused":
            if not self.refusals:
                raise ValueError("status refused requires non-empty refusals")
            if self.items:
                raise ValueError("status refused requires empty items")
        else:
            if self.refusals:
                raise ValueError("status ok requires empty refusals")
        return self
