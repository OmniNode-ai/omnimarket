# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation check regression event, shared by the probe-run regression alarm and the per-build check node.

One event is one green to red transition of one delegation check. Field names, types and the dedupe key are the ones the
probe-run regression alarm's own event already uses (event_id, check_id, repository, workflow, last_green, first_red,
changes, owner, alarm_lane, detected_at, alarm_latency_seconds, late, recovered_at), so a consumer reads both nodes' events
through one model. The per-build node adds the build it read (``build_id``, ``previous_build_id``) and the merges that build
added (``suspects``); a probe-run event leaves them empty and names its changes in ``changes``.

The dedupe key is ``event_id``: ``regression_event_id(check_id, first_red.ref_or_run)``. A check that was already red on the
previous reading has no green to red transition and so no event.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

CheckId = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")]
Repository = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
Lane = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumChangeKind(StrEnum):
    MERGE = "merge"
    DEV_BUILD = "dev_build"
    HOST_CHANGE = "host_change"
    MODEL_SETTING = "model_setting"


class EnumRegressionSource(StrEnum):
    #: Read from a probe's own finished runs (the regression alarm).
    PROBE_RUN = "probe_run"
    #: Read from one run of the check against one dev build (the per-build check node).
    DEV_BUILD = "dev_build"


class ModelRunRef(_Frozen):
    """One reading of a check. ``ref`` is the build id for a per-build reading and the head the run read for a probe run."""

    run_id: str = Field(min_length=1)
    at: AwareDatetime
    ref: str = ""


class ModelChange(_Frozen):
    """Something that changed between two readings."""

    kind: EnumChangeKind
    at: AwareDatetime
    ref: str = ""
    summary: str = Field(min_length=1)


class ModelSuspectMerge(_Frozen):
    """A merge the build added: a candidate cause of the check going red, never proof of cause."""

    commit_sha: str = Field(min_length=1)
    #: None for a direct push: no pull request record exists for it.
    pr_number: int | None = Field(default=None, ge=1)
    merged_at: AwareDatetime
    summary: str = ""


class ModelRegressionEvent(_Frozen):
    event_id: str = Field(min_length=1)
    check_id: CheckId
    repository: Repository
    workflow: str = Field(min_length=1)
    last_green: ModelRunRef
    first_red: ModelRunRef
    changes: tuple[ModelChange, ...] = ()
    #: A regression is always owned: an event with no owner is refused when it is built, never sent unowned.
    owner: str = Field(min_length=1)
    alarm_lane: Lane
    detected_at: AwareDatetime
    #: Seconds from the red reading to the detection that raised the alarm.
    alarm_latency_seconds: float = Field(ge=0)
    #: True when the alarm was raised later than the latency bound allows (a late read or a replay).
    late: bool = False
    #: The next green reading, when the check had already recovered at detection.
    recovered_at: AwareDatetime | None = None
    source: EnumRegressionSource = EnumRegressionSource.PROBE_RUN
    build_id: str = ""
    previous_build_id: str = ""
    #: The merges the red build added over the last green build, oldest first.
    suspects: tuple[ModelSuspectMerge, ...] = ()


def regression_event_id(check_id: str, red_ref: str) -> str:
    """The dedupe key: one event per check per first red reading."""
    return f"{check_id}:{red_ref}"


__all__ = [
    "EnumChangeKind",
    "EnumRegressionSource",
    "ModelChange",
    "ModelRegressionEvent",
    "ModelRunRef",
    "ModelSuspectMerge",
    "regression_event_id",
]
