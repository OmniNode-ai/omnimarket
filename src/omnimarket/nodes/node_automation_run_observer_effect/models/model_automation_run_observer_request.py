# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One poll of a host's declared automation evidence."""

from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from omnimarket.models.liveness.model_automation_liveness import NonEmptyStr

#: A host id as the overlay spells it; it becomes part of process ids.
HostId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]*$")]


class ModelUndeclaredCensusScope(BaseModel):
    """What makes a unit ours, supplied by the installer from its contracts.

    Nothing is ours when every field is empty, so the shipped observer reports
    no UNDECLARED unit until a deployment states what it owns.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    label_prefixes: tuple[NonEmptyStr, ...] = Field(
        default=(), description="A launchd label starting with one of these is ours."
    )
    known_labels: tuple[NonEmptyStr, ...] = Field(
        default=(),
        description="Labels named in a unit template or installer contract.",
    )
    path_roots: tuple[NonEmptyStr, ...] = Field(
        default=(),
        description="A unit or cron line running a path under one of these is ours.",
    )
    launchd_agent_dirs: tuple[NonEmptyStr, ...] = Field(
        default=(),
        description="Directories of launchd plists, read for each label's program.",
    )
    cron_files: tuple[NonEmptyStr, ...] = Field(
        default=(), description="Cron files whose active lines are census candidates."
    )

    @property
    def is_empty(self) -> bool:
        return not (
            self.label_prefixes
            or self.known_labels
            or self.path_roots
            or self.cron_files
        )


class ModelAutomationRunObserverRequest(BaseModel):
    """The caller names the host, the overlay, the state and the time.

    The handler reads no clock and no environment variable; every deployment
    fact it acts on arrives through the overlay file or this request.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: HostId = Field(description="This host's id in the overlay.")
    overlay_path: NonEmptyStr = Field(
        description="The automation-liveness overlay this host reads."
    )
    state_dir: NonEmptyStr = Field(
        description="Directory holding the cursors and the on-disk event journal."
    )
    now: AwareDatetime = Field(description="Time of this poll.")
    launchd_domain: NonEmptyStr | None = Field(
        default=None,
        description="launchctl domain target, e.g. gui/<uid>; a launchd entry "
        "reads UNOBSERVABLE without it.",
    )
    census: ModelUndeclaredCensusScope = Field(
        default_factory=ModelUndeclaredCensusScope
    )
