# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Types of the andon stop: the record, its events, the contract config and the gate.

Shared by the stop reducer node, the exception queue that persists stops, the rule
module ``rules_stop_gate`` and every dispatcher that asks ``stop_gate`` in process.
Nothing here names a repository, a host, an engine or an organization: the names
arrive as data (the class registry in the reducer's contract, the scope in the
request, the mode in the ``andon_stop`` overlay).
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

OPEN_ROLE_KEY = "open"
# The scope kinds a dispatcher asks about; a stop's own kind is the registry's ``scope_key_kind``.
ENGINE_SCOPE_KIND = "engine"
REPOSITORY_SCOPE_KIND = "repository"
CLASS_ID_PATTERN = r"[a-z0-9][a-z0-9-]{1,59}"


class EnumStopEventKind(StrEnum):
    """What a writer reports to a stop; ``readback`` is classified by the reducer."""

    LEASE_GRANTED = "lease_granted"
    LEASE_EXPIRED = "lease_expired"
    STALE_LIMIT_REACHED = "stale_limit_reached"
    COUNTERMEASURE_MERGED = "countermeasure_merged"
    HAND_FIX_RECORDED = "hand_fix_recorded"
    REPLACEMENTS_EXTENDED = "replacements_extended"
    READBACK = "readback"
    DECLARED_FALSE = "declared_false"


class EnumStopRole(StrEnum):
    DETECTOR = "detector"
    SWARM_LANE = "swarm_lane"
    READBACK = "readback"
    OPERATOR = "operator"


class EnumStopMode(StrEnum):
    """Per class, from the ``andon_stop`` overlay: ``observe`` names the stop, ``enforce`` refuses."""

    OBSERVE = "observe"
    ENFORCE = "enforce"


class EnumStopGateOutcome(StrEnum):
    ADMIT = "admit"
    REFUSE = "refuse"


class EnumStopGateReason(StrEnum):
    NO_STOP = "no_stop"
    STOP_OPEN = "stop_open"
    OBSERVED = "observed"
    SWARM_LANE = "swarm_lane"
    MODE_MISSING = "mode_missing"
    EXEMPT = "exempt"


class EnumStopExemptionKind(StrEnum):
    """Work a stop never refuses (it refuses new dispatch only): fixing or landing an open PR, a read-only lane, an armed merge."""

    PR_FIX = "pr_fix"
    READ_ONLY = "read_only"
    ARMED_MERGE = "armed_merge"


class EnumReadingVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"


class EnumReadingOrigin(StrEnum):
    EXECUTED = "executed"
    CACHED = "cached"
    INFERRED = "inferred"


class EnumFloorVerdict(StrEnum):
    """What the floor's own reading of a piece or scope says; only ``WORKING`` can release a stop."""

    WORKING = "WORKING"
    BROKEN = "BROKEN"
    UNKNOWN = "UNKNOWN"


class EnumReleaseReason(StrEnum):
    """Why the release predicate holds a stop open ('met' when it does not)."""

    MET = "met"
    GO_AND_SEE_MISSING = "go_and_see_missing"
    GO_AND_SEE_EMPTY = "go_and_see_empty"
    GO_AND_SEE_BEFORE_STOP = "go_and_see_before_stop"
    WORKING_READING_MISSING = "working_reading_missing"
    WORKING_READING_NOT_WORKING = "working_reading_not_working"
    WORKING_READING_BEFORE_STOP = "working_reading_before_stop"
    WORKING_READING_BEFORE_LEASE = "working_reading_before_lease"


class EnumFoldStatus(StrEnum):
    APPLIED = "applied"
    REFUSED = "refused"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelStopClass(_Frozen):
    """One entry of the class registry: ``id`` is the kebab slug the queue accepts, ``label`` is for reading."""

    id: str
    label: str
    detector: str
    scope_key_kind: str

    @model_validator(mode="after")
    def _id_is_a_kebab_slug(self) -> ModelStopClass:
        if not re.fullmatch(CLASS_ID_PATTERN, self.id):
            raise ValueError(f"stop class id {self.id!r} must be a kebab slug")
        return self


class ModelAndonStopConfig(_Frozen):
    """The ``config.andon_stop`` block of the reducer's contract: every number of the stop."""

    stale_after_hours: int = Field(gt=0)
    storm_distinct_keys: int = Field(gt=0)
    lease_minutes: int = Field(gt=0)
    max_lease_replacements: int = Field(ge=0)
    consecutive_fixed_readings: int = Field(gt=0)
    canary_dispatches_per_readback_interval: int = Field(ge=0)
    recovery_minimum_crossings: int = Field(gt=0)
    stop_classes: tuple[ModelStopClass, ...]
    writer_roles: dict[str, EnumStopRole]

    @model_validator(mode="after")
    def _registry_is_strict(self) -> ModelAndonStopConfig:
        for field in ("id", "label"):
            values = [getattr(entry, field) for entry in self.stop_classes]
            repeated = sorted({v for v in values if values.count(v) > 1})
            if repeated:
                raise ValueError(f"stop_classes repeats {field} {repeated}")
        known = {kind.value for kind in EnumStopEventKind} | {OPEN_ROLE_KEY}
        if set(self.writer_roles) != known:
            raise ValueError(
                f"writer_roles must name exactly {sorted(known)}; "
                f"missing {sorted(known - set(self.writer_roles))}, "
                f"unknown {sorted(set(self.writer_roles) - known)}"
            )
        return self

    def stop_class(self, class_id: str) -> ModelStopClass | None:
        return next((c for c in self.stop_classes if c.id == class_id), None)


class ModelStopScope(_Frozen):
    """What a stop covers: the kind (repository, host, engine, seam, ruling) and the key within it."""

    kind: str
    key: str


class ModelStopRecord(_Frozen):
    """One stop as the queue holds it: the state, the lease and the counters the guards read."""

    id: str
    cls: str
    scope: ModelStopScope
    state: str  # a state name of the contract's machine; the machine alone says which are terminal
    stopped_at: datetime
    readback_command: str = ""
    swarm_lane: str | None = None
    lease_expires_at: datetime | None = None
    lease_replacements_used: int = 0
    replacements_extended: int = 0
    countermeasure_at: datetime | None = None
    fixed_at: datetime | None = None
    fixed_readings_seen: int = 0
    cleared_at: datetime | None = None
    last_lease_at: datetime | None = (
        None  # when the newest lease (a grant or a replacement) began
    )


class ModelWorkingReading(_Frozen):
    """The floor's reading of the stopped scope that came with a readback: its verdict and when it was probed."""

    verdict: EnumFloorVerdict
    read_at: datetime


class ModelGoAndSeeRow(_Frozen):
    """The row a lane writes after it went and looked at the stopped scope itself: what it saw, where and when."""

    row_id: str = Field(min_length=1)
    lane_id: str = Field(min_length=1)
    seen_at: datetime
    observed: str


class ModelStopReading(_Frozen):
    """One run of the stop's readback command."""

    verdict: EnumReadingVerdict
    proof_eligible: bool = False
    origin: EnumReadingOrigin = EnumReadingOrigin.INFERRED
    covers_scope: bool = False
    working: ModelWorkingReading | None = None


class ModelHandFixEvidence(_Frozen):
    """The facts ``hand_fix_allowed`` is computed from; the failure record's own closure."""

    closed_fixed_by_hand: bool
    first_occurrence: bool
    repair_evidence: str = ""


class ModelStopEvent(_Frozen):
    """One writer's report to one stop."""

    stop_id: str
    kind: EnumStopEventKind
    role: str
    at: datetime
    lane_id: str = ""
    reading: ModelStopReading | None = None
    hand_fix: ModelHandFixEvidence | None = None
    extension: int = 0
    go_and_see: ModelGoAndSeeRow | None = None


class ModelStopReleaseVerdict(_Frozen):
    """The release predicate's answer for one readback: met or not, and the one reason it is not."""

    met: bool
    reason: EnumReleaseReason


class ModelStopReleaseRecord(_Frozen):
    """The typed record of a release: the go-and-see row and the WORKING reading a stop was cleared on."""

    stop_id: str
    go_and_see_row_id: str
    go_and_see_lane_id: str
    go_and_see_seen_at: datetime
    working_read_at: datetime
    released_at: datetime


class ModelStopFoldResult(_Frozen):
    """The stop after an event, the machine's transition and the actions the queue must persist."""

    status: EnumFoldStatus
    stop: ModelStopRecord
    reason: str = ""
    trigger: str = ""
    transition: str = ""
    actions: tuple[str, ...] = ()
    line: str = ""
    release: ModelStopReleaseRecord | None = None
    release_reason: EnumReleaseReason | None = None


class ModelStopFoldRequest(_Frozen):
    """One event for one stop: the reducer folds it through the contract's machine."""

    stop: ModelStopRecord
    event: ModelStopEvent


class ModelStopPolicy(_Frozen):
    """The ``andon_stop`` overlay as the gate reads it."""

    modes: dict[str, EnumStopMode]
    enforce_citations: dict[str, str] = Field(default_factory=dict)


class ModelStopExemptionClaim(_Frozen):
    """What a dispatch says it is: ``ref`` is the PR it fixes or lands, or the lane id of a read-only lane.

    A claim alone admits nothing; ``stop_gate`` honours it only against a ``ModelStopBinding`` the
    dispatcher or the PR watcher wrote.
    """

    kind: EnumStopExemptionKind
    ref: str = Field(min_length=1)


class ModelStopBinding(_Frozen):
    """One exemption the dispatcher bound: an open or armed PR read from the PR watcher state, or a read-only lane it started."""

    kind: EnumStopExemptionKind
    ref: str = Field(min_length=1)
    expires_at: datetime | None = None


class ModelStopGateRequest(_Frozen):
    """Facts for ``stop_gate``: the dispatch's scope, the open stops, the overlay policy and now."""

    scope: ModelStopScope
    stops: tuple[ModelStopRecord, ...]
    policy: ModelStopPolicy
    now: datetime
    lane_id: str = ""
    exemption: ModelStopExemptionClaim | None = None
    bindings: tuple[ModelStopBinding, ...] = ()


class ModelStopGateFacts(_Frozen):
    """What a dispatching node is handed to ask ``stop_gate``: the open stops (read by the caller from the
    exception queue), the ``andon_stop`` overlay policy (loaded by the caller), now, and the dispatch's lane and
    exemption. A node given none of this gates nothing, as before the gate was installed."""

    stops: tuple[ModelStopRecord, ...] = ()
    policy: ModelStopPolicy
    now: datetime
    lane_id: str = ""
    exemption: ModelStopExemptionClaim | None = None
    bindings: tuple[ModelStopBinding, ...] = ()


class ModelStopGateResult(_Frozen):
    outcome: EnumStopGateOutcome
    reason: EnumStopGateReason
    stop_id: str | None = None
    swarm_lane: str | None = None
    stop_class: str | None = None
    mode: EnumStopMode | None = None
    exemption: EnumStopExemptionKind | None = None
    line: str
