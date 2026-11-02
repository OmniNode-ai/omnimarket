# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The omnidash host node's schedule, host rows, update command, status file and results.

The node hosts omnidash on a lab host from the dashboard repository's own scripts and keeps it on the
newest remote branch. Every host fact (ssh target, bind address, port, repository URL, build root, node
version, poll interval, status path, bus lane) is a deployment fact: the node overlay
(``<root>/node_omnidash_host_effect/overlay.yaml``) supplies it and the package ships none. The node
contract's ``schedule`` block owns only the non-host defaults, among them the poll interval.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
UnitName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
Sha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]

UpdateOutcome = Literal[
    "switched",
    "unchanged",
    "refused-build",
    "refused-health",
    "refused-already",
    "no-branches",
    "poll-failed",
]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelOmnidashHostSchedule(_Frozen):
    """The contract's ``schedule`` block: non-host defaults of the service and its updater."""

    service_unit: UnitName
    updater_unit: UnitName
    # Seconds between two polls of the repository origin; the overlay row may override it per host.
    poll_interval_seconds: int = Field(gt=0)
    # Delay between the timer being enabled and the first poll.
    first_run_delay_seconds: int = Field(gt=0)
    build_timeout_seconds: int = Field(gt=0)
    health_timeout_seconds: int = Field(gt=0)
    health_path: NonEmptyStr
    # Old builds kept beside the one being served and its predecessor.
    keep_builds: int = Field(ge=0)
    mirror_directory: NonEmptyStr
    current_link: NonEmptyStr
    schedulers: dict[Literal["systemd-user"], NonEmptyStr] = Field(min_length=1)


class ModelOmnidashHostRow(_Frozen):
    """One host that serves omnidash; every field is a deployment fact."""

    name: NonEmptyStr
    scheduler: Literal["systemd-user"] = "systemd-user"
    ssh_target: NonEmptyStr
    # The address the service binds to: the host's tailnet address, never a wildcard.
    bind_address: NonEmptyStr
    port: int = Field(gt=0, lt=65536)
    # A free port the candidate instance of a build is health-checked on before any switch.
    scratch_port: int = Field(gt=0, lt=65536)
    # Where the page answers; its host name is the name the dev server must accept.
    serve_url: NonEmptyStr
    repo_url: NonEmptyStr
    build_root: NonEmptyStr
    node_version: Annotated[str, StringConstraints(pattern=r"^\d+\.\d+\.\d+$")]
    # Absolute directory the pinned node distribution is unpacked under (user space).
    tools_root: NonEmptyStr
    # Base URL of the node distribution mirror the pinned node is downloaded from.
    node_dist_base: NonEmptyStr
    status_path: NonEmptyStr
    bus_lane: NonEmptyStr
    # The lane's projection API origin and the tenant the dashboard reads as.
    projection_api_url: NonEmptyStr
    projection_tenant_id: NonEmptyStr
    poll_interval_seconds: int | None = Field(default=None, gt=0)
    # Directory holding the omnimarket ``src`` the updater runs; empty means the dispatch venv's own.
    omnimarket_src: str = ""
    # Dispatch venv interpreter the updater unit runs with.
    python: NonEmptyStr


class ModelOmnidashHostOverlay(_Frozen):
    """The node overlay: every host that serves omnidash."""

    hosts: Annotated[tuple[ModelOmnidashHostRow, ...], Field(min_length=1)]


class ModelOmnidashBranchTip(_Frozen):
    """One remote branch and the committer time of its tip."""

    branch: NonEmptyStr
    sha: Sha
    committed_at: int  # unix seconds


class ModelOmnidashRefusal(_Frozen):
    branch: NonEmptyStr
    sha: Sha
    reason: NonEmptyStr


class ModelOmnidashHostStatus(_Frozen):
    """The status file: what is served, when it was switched, and why the newest branch was refused."""

    served_branch: str | None = None
    served_sha: str | None = None
    served_committed_at: int | None = None
    switched_at: str | None = None
    last_poll_at: str | None = None
    last_result: str | None = None
    newest_branch: str | None = None
    newest_sha: str | None = None
    # Set when the newest branch was not switched to; the last good build keeps serving.
    refusal_reason: str | None = None
    refused_sha: str | None = None


class ModelOmnidashHostUpdateCommand(_Frozen):
    """One updater tick for one host row."""

    host: ModelOmnidashHostRow
    schedule: ModelOmnidashHostSchedule
    # Serve exactly this commit instead of the newest branch tip (a starting state for a proof).
    pin_sha: Sha | None = None


class ModelOmnidashHostUpdateResult(_Frozen):
    outcome: UpdateOutcome
    status: ModelOmnidashHostStatus
    detail: str = ""


class ModelOmnidashHostInstallResult(_Frozen):
    """The outcome of a schedule verb: a process exit code, what to print and whether anything changed."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    changed: bool = False
