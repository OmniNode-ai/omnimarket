# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result of the hook install emission judge (OMN-26658).

The request carries everything the judge reads: the declared installs, the event kinds each must put on the bus
(each kind a name and the hook event names that satisfy it), the events read back from the hook-event projection and
the window start. Nothing is read from a host or a table here.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

#: The tools whose events the projection's `actor` column names (the capture contract's actor envelope).
#: The tool is a name the request declares, never a literal here (OMN-26651): the hard-coded model guard refuses an engine label in source.
Tool = Annotated[str, StringConstraints(min_length=1)]
Actor = Annotated[str, StringConstraints(min_length=1)]
InstallStatus = Literal["emitting", "partial", "silent"]


class ModelHookInstallDeclared(BaseModel):
    """One install on a machine we own: the host and the tool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1, description="The lab host's declared name")
    tool: Tool
    host_aliases: tuple[str, ...] = Field(
        default=(),
        description="Other names the machine reports as its hostname (a lab host's native name)",
    )
    last_session_at: AwareDatetime | None = Field(
        default=None,
        description="When the install last ran a session (read by the caller from the ledger lane rows); None when it ran none",
    )


class ModelHookInstallRequiredKind(BaseModel):
    """One event kind an install must emit, and the hook event names that count as it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1)
    hook_event_names: tuple[str, ...] = Field(min_length=1)


class ModelHookInstallObservedEvent(BaseModel):
    """One row read back from the hook-event projection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    actor: Actor
    hook_event_name: str = Field(min_length=1)
    emitted_at: AwareDatetime


class ModelHookInstallEmissionRequest(BaseModel):
    """The declared installs, the required kinds, the events read back and the window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    installs: tuple[ModelHookInstallDeclared, ...]
    required_kinds: tuple[ModelHookInstallRequiredKind, ...] = Field(min_length=1)
    events: tuple[ModelHookInstallObservedEvent, ...] = ()
    since: AwareDatetime = Field(description="Events before this instant do not count")
    read_at: AwareDatetime = Field(description="When the projection was read")
    quiet_minutes: int = Field(
        default=30,
        ge=1,
        description="An install that ran a session and emitted nothing for this long raises an alert",
    )

    @model_validator(mode="after")
    def _install_declared_once(self) -> ModelHookInstallEmissionRequest:
        seen: set[tuple[str, str]] = set()
        for install in self.installs:
            key = (install.host.casefold(), install.tool)
            if key in seen:
                raise ValueError(
                    f"install {install.host}/{install.tool} declared twice"
                )
            seen.add(key)
        return self


class ModelHookInstallVerdict(BaseModel):
    """What one install put on the bus inside the window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    tool: Tool
    status: InstallStatus
    kinds_seen: tuple[str, ...]
    kinds_missing: tuple[str, ...]
    event_count: int = Field(ge=0)
    newest_event_at: AwareDatetime | None = None


class ModelHookInstallUndeclared(BaseModel):
    """An emitter in the window that no declared install explains."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    actor: Actor
    event_count: int = Field(ge=1)


class ModelHookInstallAlert(BaseModel):
    """One install that ran a session and put nothing on the bus since."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    tool: Tool
    session_at: AwareDatetime
    newest_event_at: AwareDatetime | None = None


class ModelHookInstallEmissionResult(BaseModel):
    """One verdict per declared install, the silent ones, and the emitters nobody declared."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    installs: tuple[ModelHookInstallVerdict, ...]
    silent_installs: tuple[ModelHookInstallVerdict, ...]
    undeclared: tuple[ModelHookInstallUndeclared, ...]
    all_emitting: bool
    read_at: AwareDatetime
    alerts: tuple[ModelHookInstallAlert, ...] = ()
    alert_text: str = ""
