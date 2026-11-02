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


class EnumReadingVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"


class EnumReadingOrigin(StrEnum):
    EXECUTED = "executed"
    CACHED = "cached"
    INFERRED = "inferred"


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


class ModelStopReading(_Frozen):
    """One run of the stop's readback command."""

    verdict: EnumReadingVerdict
    proof_eligible: bool = False
    origin: EnumReadingOrigin = EnumReadingOrigin.INFERRED
    covers_scope: bool = False


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


class ModelStopFoldResult(_Frozen):
    """The stop after an event, the machine's transition and the actions the queue must persist."""

    status: EnumFoldStatus
    stop: ModelStopRecord
    reason: str = ""
    trigger: str = ""
    transition: str = ""
    actions: tuple[str, ...] = ()
    line: str = ""


class ModelStopFoldRequest(_Frozen):
    """One event for one stop: the reducer folds it through the contract's machine."""

    stop: ModelStopRecord
    event: ModelStopEvent


class ModelStopPolicy(_Frozen):
    """The ``andon_stop`` overlay as the gate reads it."""

    modes: dict[str, EnumStopMode]
    enforce_citations: dict[str, str] = Field(default_factory=dict)


class ModelStopGateRequest(_Frozen):
    """Facts for ``stop_gate``: the dispatch's scope, the open stops, the overlay policy and now."""

    scope: ModelStopScope
    stops: tuple[ModelStopRecord, ...]
    policy: ModelStopPolicy
    now: datetime
    lane_id: str = ""


class ModelStopGateResult(_Frozen):
    outcome: EnumStopGateOutcome
    reason: EnumStopGateReason
    stop_id: str | None = None
    swarm_lane: str | None = None
    stop_class: str | None = None
    mode: EnumStopMode | None = None
    line: str
