# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed overlay, command, events and result of the lab daemon sync (OMN-19998)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime
from string import Formatter
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_host_layer import ModelLayerBounds
from omnimarket.models.model_lab_daemon_drift import ModelLabDaemonDrift

_TS = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"


def _valid_stamp(value: str) -> str:
    datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    return value


_Arg = Annotated[str, Field(min_length=1)]
REPO_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
FIX_LANE_FIELDS = frozenset({"lane", "brief", "daemon", "host"})
_Stamp = Annotated[str, Field(pattern=_TS), AfterValidator(_valid_stamp)]
_Slug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]*$")]
_Role = Literal["lab-proof", "lab", "launcher"]
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_VARIABLE_PREFIX = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}")
_HOST_LITERAL = re.compile(
    r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])|[A-Za-z0-9._-]+@[A-Za-z0-9._-]+"
)


def _string_cells(value: object) -> Iterator[str]:
    """Visit string values in a serialized declaration."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _string_cells(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _string_cells(item)


EnumDaemonDecision = Literal[
    "current", "lagging", "branch_expired", "branch_held", "unreadable"
]
EnumDaemonVerdict = Literal[
    "current",
    "lagging",
    "installed",
    "branch_held",
    "busy",
    "refused",
]
EnumCloneVerdict = Literal[
    "current",
    "missing",
    "behind",
    "created",
    "fast_forwarded",
    "dirty",
    "off_branch",
    "diverged",
    "refused",
]
EnumChoreVerdict = Literal["ran", "due", "refused"]


class ModelRevisionProbe(BaseModel):
    """How to read one revision: a command's stripped stdout, or a key of a JSON file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["command", "json_file"]
    argv: list[_Arg] = Field(default_factory=list)
    path: str | None = None
    key: str | None = Field(
        default=None,
        description=(
            "Dotted key (a number indexes a list) into the JSON file for `json_file`, or into the "
            "command's stdout parsed as JSON for `command`; omitted for a command means its last "
            "stdout line."
        ),
    )

    @model_validator(mode="after")
    def validate_probe(self) -> ModelRevisionProbe:
        if self.kind == "command" and not self.argv:
            raise ValueError("command probe requires non-empty argv")
        if self.kind == "json_file" and not self.path:
            raise ValueError("json_file probe requires a non-empty path")
        return self


class ModelSurfaceSetting(BaseModel):
    """One Claude configuration setting owned by a surface."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    value: bool | str


class ModelSurfaceEnv(BaseModel):
    """One environment change owned by a launchd surface."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    action: Literal["unset", "set"]
    value: str | None = None

    @model_validator(mode="after")
    def _action_value(self) -> ModelSurfaceEnv:
        if (self.action == "set") != (self.value is not None):
            raise ValueError("env value is required exactly when action is set")
        return self


class ModelSurfaceDeclaration(BaseModel):
    """One portable installation, configuration or observed revision surface."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: _Slug
    kind: Literal[
        "lane_tree", "installed_plugin", "claude_config", "launchd_env", "observed"
    ]
    description: str = ""
    applies_to_roles: tuple[_Role, ...] = Field(min_length=1)
    repository: (
        Annotated[
            str,
            Field(pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9._-]*|\$\{[A-Z][A-Z0-9_]*\})$"),
        ]
        | None
    ) = None
    branch: str = "main"
    repository_paths: tuple[str, ...] = ()
    installed: ModelRevisionProbe | None = None
    loaded: ModelRevisionProbe | None = None
    runtime: _Slug | None = None
    settings: tuple[ModelSurfaceSetting, ...] = ()
    env: tuple[ModelSurfaceEnv, ...] = ()

    @model_validator(mode="after")
    def _portable_string_cells(self) -> ModelSurfaceDeclaration:
        """Refuse host literals in paths and probe argv before variable expansion."""
        cells = list(self.repository_paths)
        for path in self.repository_paths:
            if path.startswith("/") or ".." in path.split("/") or "~" in path:
                raise ValueError(
                    "repository_paths must stay relative inside the repository"
                )
        for probe in (self.installed, self.loaded):
            if probe is not None:
                if probe.path is not None:
                    cells.append(probe.path)
                cells.extend(probe.argv)
        for cell in cells:
            if _VARIABLE_PREFIX.match(cell):
                continue
            if cell.startswith(("/", "~")) or _HOST_LITERAL.search(cell):
                raise ValueError(
                    "surface paths and argv must not contain host literals"
                )
        return self

    @model_validator(mode="after")
    def _kind_payload(self) -> ModelSurfaceDeclaration:
        if self.kind == "lane_tree":
            if self.runtime is None:
                raise ValueError("lane_tree requires runtime")
            if "launcher" in self.applies_to_roles:
                raise ValueError("launcher cannot use a lane_tree")
        elif self.runtime is not None:
            raise ValueError("runtime is only allowed for lane_tree")
        if self.kind in ("lane_tree", "installed_plugin") and (
            self.repository is None or self.installed is None
        ):
            raise ValueError(f"{self.kind} requires repository and installed")
        if self.kind == "observed" and self.loaded is None:
            raise ValueError("observed requires loaded")
        if bool(self.settings) != (self.kind == "claude_config"):
            raise ValueError("non-empty settings are required only for claude_config")
        if bool(self.env) != (self.kind == "launchd_env"):
            raise ValueError("non-empty env is required only for launchd_env")
        return self

    def variables(self) -> tuple[str, ...]:
        """Return every referenced environment variable in sorted, unique order."""
        return tuple(
            sorted(
                {
                    match.group(1) or match.group(2)
                    for cell in _string_cells(self.model_dump())
                    for match in _VARIABLE.finditer(cell)
                }
            )
        )


class ModelTransactionalId(BaseModel):
    """A transactional producer identity, optionally scoped to one host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: _Slug
    id: str
    per_host: bool

    @model_validator(mode="after")
    def _host_template(self) -> ModelTransactionalId:
        if self.per_host:
            if self.id.count("{host}") != 1:
                raise ValueError("per_host id requires exactly one {host} placeholder")
        elif "{" in self.id or "}" in self.id:
            raise ValueError("id without per_host must not contain braces")
        return self


class ModelBranchRecord(BaseModel):
    """The install record a branch install writes its expiry into (an installer --branch-hours)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    until_key: str = "branch_until"
    source_key: str = "branch_source"


class ModelDaemonDeclaration(BaseModel):
    """One lab daemon, as the overlay declares it. Nothing about a daemon lives in the node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str = ""
    hosts: list[str] = Field(
        default_factory=lambda: ["*"],
        description="Host names this daemon runs on; `*` is every host.",
    )
    present_when: str | None = Field(
        default=None,
        description="A path that exists only where the daemon is installed; absent skips the host.",
    )
    source_branch: str = "main"
    installed: ModelRevisionProbe
    source: ModelRevisionProbe
    installer: list[_Arg] = Field(
        min_length=1,
        description="The daemon's own installer, which converges it to the source branch tip.",
    )
    installer_timeout_s: int = Field(default=1800, ge=1)
    locks: list[str] = Field(
        default_factory=list,
        description="Locks the installer takes itself; any held one skips this tick as busy.",
    )
    branch_record: ModelBranchRecord | None = None
    absent_is_lag: bool = Field(
        default=False,
        description=(
            "An unreadable installed revision is a lag the installer fixes (a plugin this host "
            "does not have yet), not a refusal."
        ),
    )


class ModelCloneSet(BaseModel):
    """The registry clones every lab host holds: a missing one is cloned, a clean one fast-forwarded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root: str = Field(
        min_length=1, description="The registry root the clones live under."
    )
    url: str = Field(
        pattern=r"^https://[^\s{}]*\{repo\}[^\s{}]*$",
        description="HTTPS clone URL template; `{repo}` is the repository name.",
    )
    repos: list[Annotated[str, Field(pattern=REPO_NAME_PATTERN)]] = Field(min_length=1)
    extra_repos_file: str | None = Field(
        default=None,
        description='A host JSON file {"repos": [...]} naming more clones, such as private ones.',
    )
    timeout_s: int = Field(default=900, ge=1)


class ModelChoreDeclaration(BaseModel):
    """A host maintenance command the sync runs every tick, such as the pre-commit cache gc."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str = ""
    hosts: list[str] = Field(default_factory=lambda: ["*"])
    present_when: str | None = None
    argv: list[_Arg] = Field(min_length=1)
    timeout_s: int = Field(default=600, ge=1)
    no_change: str | None = Field(
        default=None,
        description="A regular expression the output matches when the run changed nothing.",
    )


class ModelDaemonOverlay(BaseModel):
    """Declared daemons, portable surfaces, layer bounds and fix-lane command."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1, 2]
    daemons: list[ModelDaemonDeclaration]
    fix_lane: list[_Arg] = Field(
        min_length=1,
        description=(
            "argv template that dispatches one detached fix lane; `{lane}`, `{brief}`, `{daemon}` "
            "and `{host}` are filled per refusal."
        ),
    )
    clones: ModelCloneSet | None = None
    chores: list[ModelChoreDeclaration] = Field(default_factory=list)
    fix_lane_repeat_hours: float = Field(
        default=6.0,
        gt=0,
        description="The same blocker dispatches another fix lane only after this long.",
    )
    surfaces: list[ModelSurfaceDeclaration] = Field(default_factory=list)
    layers: ModelLayerBounds | None = None
    transactional_ids: list[ModelTransactionalId] = Field(default_factory=list)

    @model_validator(mode="after")
    def _version_fields(self) -> ModelDaemonOverlay:
        if self.schema_version == 1:
            if self.surfaces or self.layers is not None or self.transactional_ids:
                raise ValueError(
                    "schema_version 1 requires empty surfaces, layers and transactional_ids"
                )
        elif not self.surfaces or self.layers is None or not self.transactional_ids:
            raise ValueError(
                "schema_version 2 requires surfaces, layers and transactional_ids"
            )
        names = [surface.name for surface in self.surfaces]
        if len(names) != len(set(names)):
            raise ValueError("surface names must be unique")
        roles = [entry.role for entry in self.transactional_ids]
        if len(roles) != len(set(roles)):
            raise ValueError("transactional id roles must be unique")
        return self

    @model_validator(mode="after")
    def _fix_lane_placeholders(self) -> ModelDaemonOverlay:
        """A fix-lane part names only the four plain fields, so formatting it cannot fail or reach an attribute; `${VAR}` is expanded at load."""
        for part in self.fix_lane:
            for text, field, spec, conversion in Formatter().parse(part):
                if field is None or text.endswith("$"):
                    continue
                if field not in FIX_LANE_FIELDS or spec or conversion:
                    raise ValueError(
                        f"fix_lane part {part!r} names {{{field}}}; "
                        f"only {', '.join(sorted(FIX_LANE_FIELDS))} are filled, with no spec or conversion"
                    )
        return self


class ModelLabDaemonSyncCommand(BaseModel):
    """One host tick of the sync."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    overlay: ModelDaemonOverlay
    state_dir: str = Field(min_length=1)
    now: _Stamp
    mode: Literal["repair", "check"] = "repair"


class ModelDaemonFacts(BaseModel):
    """What the probes read for one daemon, before any decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    installed_revision: str | None = None
    installed_error: str | None = None
    source_revision: str | None = None
    source_error: str | None = None
    branch_until: str | None = None
    branch_source: str | None = None


class ModelDaemonOutcome(BaseModel):
    """What one tick decided and did for one daemon."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    daemon: str
    host: str
    decision: EnumDaemonDecision
    verdict: EnumDaemonVerdict
    installed_revision: str | None = None
    source_revision: str | None = None
    branch_until: str | None = None
    blocker: str | None = None
    fix_lane: str | None = None


class ModelCloneOutcome(BaseModel):
    """What one tick found and did for one registry clone."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    path: str
    verdict: EnumCloneVerdict
    branch: str | None = None
    before: str | None = None
    after: str | None = None
    blocker: str | None = None
    fix_lane: str | None = None


class ModelChoreOutcome(BaseModel):
    """What one tick's run of one maintenance command printed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    verdict: EnumChoreVerdict
    output: str = ""
    blocker: str | None = None
    fix_lane: str | None = None


class ModelLabDaemonInstallRefused(BaseModel):
    """The alarm event: a daemon could not be converged, naming the blocker."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    host: str
    daemon: str
    blocker: str
    installed_revision: str | None = None
    source_revision: str | None = None
    lagging_since: _Stamp
    fix_lane: str
    fix_lane_dispatched: bool
    fix_lane_error: str | None = None


class ModelLabDaemonSyncResult(BaseModel):
    """The terminal event and the CLI's stdout."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    host: str
    at: _Stamp
    mode: Literal["repair", "check"]
    daemons: list[ModelDaemonOutcome]
    drifts: list[ModelLabDaemonDrift]
    clones: list[ModelCloneOutcome] = Field(default_factory=list)
    chores: list[ModelChoreOutcome] = Field(default_factory=list)
    changes: list[str] = Field(
        default_factory=list,
        description="One line per change this run made on the host; empty when it changed nothing.",
    )
    diagnostics: list[str] = Field(default_factory=list)
    exit_code: int
