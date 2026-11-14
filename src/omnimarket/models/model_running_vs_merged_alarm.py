# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host-drift open items in, the running-vs-merged findings out (OMN-19998)."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class ModelRunningSurface(BaseModel):
    """One open item of the host-drift fleet fold: what one host runs against what it declares."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    # The fold's item key, such as plugin:omni@omninode-internal or dispatch-venv:<path>.
    key: str
    kind: str
    observed: str = ""
    first_seen_at: AwareDatetime
    verdict: str = "DRIFT"
    # A recorded branch install on purpose (install.sh --branch-hours writes branch_until and
    # branch_source into landing_install.json); None when the item carries no readable hold.
    branch_until: AwareDatetime | None = None
    branch_source: str = ""


class ModelFleetFoldAbsence(BaseModel):
    """What the declaration says about a host-drift fleet fold that does not exist (OMN-23275).

    ``owners`` are the hosts the manifest gives the host-drift unit, as ``id (hostname)``;
    ``declared_here`` is whether the reading host is one of them and ``plist_installed`` whether the
    unit's launchd plist is on the reading host. ``unit``, ``manifest``, ``installer`` and ``ticket``
    are the words of the finding the caller reads from its overlay, so the handler holds none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    this_host: str
    unit: str
    manifest: str
    installer: str
    ticket: str
    owners: tuple[str, ...] = ()
    declared_here: bool = False
    plist_installed: bool = False


class ModelBranchInstallReading(BaseModel):
    """What the dev deploy agent last read back for one install from a branch (OMN-17427)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    install_key: str
    repo: str
    pr: int
    # present, reapplied, missing or failed: the agent's own states.
    state: str
    # One "<path> expected <sha12> observed <sha12>" entry per file that is not whole.
    unwhole: tuple[str, ...] = ()


class ModelRunningVsMergedAcknowledgement(BaseModel):
    """Deliberate drift on one surface, bounded by an expiry and observed content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    surface: str = Field(min_length=1)
    by: str = Field(min_length=1)
    why: str = Field(min_length=1)
    cites: str = Field(min_length=1)
    while_observed: str = Field(min_length=1)
    acknowledged_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def validate_expiry(self) -> Self:
        if self.expires_at <= self.acknowledged_at:
            raise ValueError("expires_at must be after acknowledged_at")
        return self


class ModelHostOmniHome(BaseModel):
    """The OMNI_HOME one host declares in its own environment, read by the caller (OMN-19998)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    home: str


class ModelRunningVsMergedRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    # False when the fleet fold could not be read: the verdict is then UNKNOWN, never OK.
    fleet_readable: bool = True
    unreadable_why: str = ""
    # Set when the fold file does not exist at all: the finding then says who declares its producer.
    fleet_absence: ModelFleetFoldAbsence | None = None
    # When the fold was written; a fold older than ``fleet_max_age_minutes`` cannot vouch for now.
    folded_at: AwareDatetime | None = None
    items: tuple[ModelRunningSurface, ...] = ()
    acknowledgements: tuple[ModelRunningVsMergedAcknowledgement, ...] = ()
    # The OMNI_HOME each host declares, for the fix lines that run a tool from a host's registry
    # root; a host with no row here reads its own environment.
    host_homes: tuple[ModelHostOmniHome, ...] = ()
    # The dev deploy agent's /branch-installs readback. False when nobody asked for it, so a host
    # that does not run the dev lane reads no agent and raises nothing.
    branch_installs_requested: bool = False
    # False when it was asked for and could not be read: the verdict is then UNKNOWN, never OK.
    branch_installs_readable: bool = True
    branch_installs_why: str = ""
    # Installs the agent declares, and when it last read them back.
    branch_installs_declared: tuple[str, ...] = ()
    branch_installs_checked_at: AwareDatetime | None = None
    branch_installs: tuple[ModelBranchInstallReading, ...] = ()
    branch_installs_max_age_minutes: int = Field(default=15, gt=0)
    bound_minutes: int = Field(default=60, gt=0)
    fleet_max_age_minutes: int = Field(default=45, gt=0)


class ModelRunningVsMergedFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    level: Literal["missing", "unknown", "note"]
    # The tick's loop name: running-vs-merged:<surface>.
    loop: str
    why: str
    fix: str


class ModelRunningVsMergedHeld(BaseModel):
    """A surface off main that its installer declared and that has not expired (OMN-17427)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    loop: str
    # The hold's end, YYYY-MM-DDTHH:MM:SSZ; empty for a declaration with no end (a hot-patch).
    until: str = ""
    why: str


class ModelRunningVsMergedResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["OK", "MISSING", "UNKNOWN"]
    findings: tuple[ModelRunningVsMergedFinding, ...] = ()
    # Note-level findings: differences that are expected, such as a live recorded branch hold.
    acknowledged: tuple[ModelRunningVsMergedFinding, ...] = ()
    # Reported, never a finding: the tick dispatches a repair only for a finding.
    held: tuple[ModelRunningVsMergedHeld, ...] = ()
