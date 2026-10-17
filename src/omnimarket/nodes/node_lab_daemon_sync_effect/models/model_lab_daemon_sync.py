# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed overlay, command, events and result of the lab daemon sync (OMN-19998)."""

from __future__ import annotations

from datetime import datetime
from string import Formatter
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_lab_daemon_drift import ModelLabDaemonDrift

_TS = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"


def _valid_stamp(value: str) -> str:
    datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    return value


_Arg = Annotated[str, Field(min_length=1)]
REPO_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
FIX_LANE_FIELDS = frozenset({"lane", "brief", "daemon", "host"})
_Stamp = Annotated[str, Field(pattern=_TS), AfterValidator(_valid_stamp)]

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
    """The declared lab daemons and the fix-lane command (the remote-lane runner)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
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
