# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the C30 Claude Code plugin probe node (OMN-19966): request, one graded reading, the run record and the overlay.

The run record is shaped as the MVP launch block's ``ModelMvpRun`` (run_id, created_at, event, status, conclusion), so the
launch reading (M1) reads C30 from it under its rule "the newest scheduled run decides". Only a PASS is ``success``: a FAIL,
a PRECONDITION-ABSENT (nothing graded) and an ERROR (the probe itself broke) all conclude ``failure``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.bus_binding.model_node_event_bus import ModelNodeEventBus

#: The dashboard snapshot topic whose rows the probe reads; the node does not own it.
DECISIONS_TOPIC = "onex.snapshot.projection.delegation.decisions.v1"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumC30Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    PRECONDITION_ABSENT = "PRECONDITION-ABSENT"
    ERROR = "ERROR"


class ModelC30Clause(_Frozen):
    name: str = Field(min_length=1)
    ok: bool
    detail: str


class ModelC30Precondition(_Frozen):
    name: str = Field(min_length=1)
    present: bool
    detail: str
    owner: str


class ModelC30ProbeReading(_Frozen):
    """What a prober graded: the clauses and the surfaces it needed. Never carries a credential value."""

    preconditions: tuple[ModelC30Precondition, ...] = ()
    clauses: tuple[ModelC30Clause, ...] = ()
    # The installed version of each package the probe graded (the overlay's graded_packages), read from the onex it ran.
    versions: dict[str, str] = Field(default_factory=dict)

    def verdict(self) -> EnumC30Verdict:
        if any(not p.present for p in self.preconditions):
            return EnumC30Verdict.PRECONDITION_ABSENT
        if not self.clauses:
            # Preconditions held and nothing was graded: a defect in the probe, never a pass.
            return EnumC30Verdict.ERROR
        return (
            EnumC30Verdict.PASS
            if all(c.ok for c in self.clauses)
            else EnumC30Verdict.FAIL
        )


class ModelC30ProbeRequest(_Frozen):
    correlation_id: str = Field(min_length=1)
    # ``schedule`` is what the timer unit sends; M1 admits only the triggers its overlay names.
    trigger: Literal["schedule", "manual"] = "schedule"


class ModelC30ProbeResult(_Frozen):
    """One run of the probe, as emitted on the bus and appended to the run history."""

    correlation_id: str
    run_id: str = Field(min_length=1)
    created_at: datetime
    finished_at: datetime
    event: Literal["schedule", "manual"]
    # ``completed`` whenever the probe returned a reading; ``errored`` when it raised.
    status: Literal["completed", "errored"]
    conclusion: Literal["success", "failure"]
    verdict: EnumC30Verdict
    profile: str = Field(min_length=1)
    clauses: tuple[ModelC30Clause, ...] = ()
    preconditions: tuple[ModelC30Precondition, ...] = ()
    versions: dict[str, str] = Field(default_factory=dict)
    error: str | None = None
    emitted: bool = False

    @model_validator(mode="after")
    def _aware_and_consistent(self) -> ModelC30ProbeResult:
        if self.created_at.tzinfo is None or self.finished_at.tzinfo is None:
            raise ValueError("created_at and finished_at carry a time zone")
        if (self.conclusion == "success") != (self.verdict is EnumC30Verdict.PASS):
            raise ValueError("only a PASS concludes success")
        if (self.status == "errored") != (self.verdict is EnumC30Verdict.ERROR):
            raise ValueError("status errored if and only if the verdict is ERROR")
        return self

    def mvp_run(self) -> dict[str, object]:
        """The run in the MVP launch block's run shape (``ModelMvpRun``)."""
        return {
            "run_id": self.run_id,
            "created_at": self.created_at.isoformat(),
            "event": self.event,
            "status": self.status,
            "conclusion": self.conclusion,
            "versions": dict(self.versions),
        }


class ModelC30ProfileOverlay(_Frozen):
    kind: Literal["inmemory", "local", "tick"]
    # ``local``: the directory holding the ``onex`` the probe grades, put first on PATH for the probe's own commands.
    # ``tick``: the environment the tick itself runs in (the registry project's) is the one graded; no directory is named.
    onex_bin_dir: str | None = None
    # ``local``: the packages whose installed versions the run records, so M1 can compare them with the launch candidate.
    graded_packages: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _local_names_its_cli(self) -> ModelC30ProfileOverlay:
        if self.kind == "local" and not self.onex_bin_dir:
            raise ValueError("a local profile names onex_bin_dir")
        if self.kind == "tick" and self.onex_bin_dir:
            raise ValueError(
                "a tick profile grades its own environment and names no onex_bin_dir"
            )
        if self.kind in ("local", "tick") and not self.graded_packages:
            raise ValueError(f"a {self.kind} profile names graded_packages")
        return self


class ModelC30ProbeOverlay(_Frozen):
    """Every setting of the node; no key has a default."""

    default_profile: str = Field(min_length=1)
    profiles: dict[str, ModelC30ProfileOverlay]
    # The run history, one JSON line per run, under the tick's state directory.
    history_file: str = Field(min_length=1)
    history_keep: int = Field(gt=0)
    # The topic the run record is emitted to; it must be one the contract declares.
    result_topic: str = Field(min_length=1)
    # The bus the tick delivers its run record on: the lane id only, read by the declared broker publisher. A lab host's
    # unit carries no KAFKA_BOOTSTRAP_SERVERS, so without it the tick delivers nothing and exits 3.
    event_bus: ModelNodeEventBus | None = None

    @model_validator(mode="after")
    def _default_exists(self) -> ModelC30ProbeOverlay:
        if self.default_profile not in self.profiles:
            raise ValueError(
                f"default_profile {self.default_profile!r} is not a profile"
            )
        return self

    def profile(self, name: str | None) -> ModelC30ProfileOverlay:
        key = name or self.default_profile
        if key not in self.profiles:
            raise ValueError(f"unknown profile {key!r}")
        return self.profiles[key]
