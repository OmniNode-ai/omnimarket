# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic head-freeze contract (OMN-14644 / WS1).

Split Product Readiness from OCC evidence production and add a deterministic
head-freeze idempotency key, per the merge-flow throughput plan (epic
OMN-14643, WS1).

The problem this addresses
--------------------------
Today `omnimarket` product lint/type/test/coverage runs *downstream* of the OCC
preflight dependency, so a normal product failure can only surface *after*
head-bound OCC evidence has already been created. That converts a late product
defect into wasted evidence plus a repair companion. WS1 inverts the order:
product validation runs first with no OCC dependency, and only after it passes
is a durable freeze record emitted for the exact frozen tuple.

What this module owns
---------------------
* ``ModelFrozenTuple`` — the minimum idempotency identity of a candidate head:
  ``(repo, pr_number, ticket SET, head_sha, base_ref, contract_digest,
  policy_digest)``. Per the plan §4 hardening:
    - ``ticket_set`` is a *set*, not a single ticket (occ-preflight extracts
      multiple tickets per PR); a single-ticket tuple silently mis-keys
      multi-ticket PRs.
    - ``policy_digest`` (the pinned workflow/policy digest) is load-bearing:
      without it a "relevant configuration change" cannot be *detected* against
      the tuple, so the supersession trigger would be undetectable.
* ``ModelHeadFreezeRecord`` — the durable freeze record. It is an *idempotency
  key, not evidence*: a validator re-derives the head SHA and contract digest at
  validation time and treats the tuple strictly as a key; it never trusts a
  recorded claim. A freeze is only ever emitted for a ``PRODUCT_GREEN`` head.
* ``ModelHeadFreezeSupersession`` — the record emitted when the frozen tuple
  moves (synchronize / base retarget / contract-digest / policy-digest change),
  routing the candidate back to ``BUILD`` instead of silently reusing stale
  evidence.
* ``FreezeLedger`` — a deterministic, in-memory idempotent store proving the
  three WS1 invariants (one freeze per green head, idempotent replay,
  supersession on tuple change). The durable control-plane surface (event log /
  OCC append-only) is wired in the later enforcement PR; the ledger is the
  local/test surface and the seam that surface must satisfy.

Vocabulary reuse (hard requirement — plan §4, CLAUDE.md §"Define and match
seams", and WS6/OMN-14648's own note):

* Event/record **identity** reuses the canonical deterministic-fingerprint
  primitive
  ``omnibase_core.validation.cross_repo.util_fingerprint.generate_fingerprint``
  (the same 16-hex SHA-256 scheme WS6 used for ``event_id``) rather than a
  parallel hashing scheme.
* Only the **product-readiness outcome** and the **supersession-reason** axes
  are net-new here. Neither has an existing enum, and neither the CI-phase FSM
  (``EnumPrLifecyclePhase``) nor the receipt-correction model
  (``ModelReceiptSupersession``, which requires a ``ModelDodReceipt``
  replacement) fits a *freeze idempotency key*: a freeze supersession
  invalidates a key and routes back to ``BUILD``; there is no ``ModelDodReceipt``
  to re-bind. This is the "written reason why reuse fails" the plan requires and
  mirrors WS6's justification for its net-new ``EnumMergeRerunReason``.

REPORT-ONLY: this first PR ships the contract, the deterministic ledger, and the
report-only classifier/workflow. No enforcement gate mutates branch protection
or the merge queue.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum

from omnibase_core.validation.cross_repo.util_fingerprint import generate_fingerprint
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

# Namespace slots passed to ``generate_fingerprint`` so head-freeze fingerprints
# never collide with validation-violation or merge-state fingerprints in the
# same 16-hex space.
_TUPLE_FINGERPRINT_NAMESPACE = "head_freeze_tuple"
_SUPERSESSION_FINGERPRINT_NAMESPACE = "head_freeze_supersession"

_TICKET_ID_RE = re.compile(r"^OMN-\d+$")


class EnumProductReadinessOutcome(StrEnum):
    """Aggregate outcome of Product Readiness for one PR head.

    Only ``PRODUCT_GREEN`` is freeze-eligible. Every other value names why the
    head is not freeze-eligible so a head-bound OCC companion is never created
    against an unproven product head (the red-before-OCC invariant). The
    string values are the canonical vocabulary that the stdlib-only
    ``scripts/ci/product_readiness.py`` classifier mirrors; a parity test guards
    against drift between the two.
    """

    PRODUCT_GREEN = "product_green"
    CHANGE_DETECTION_FAILED = "change_detection_failed"
    LINT_FAILED = "lint_failed"
    TYPE_FAILED = "type_failed"
    TEST_FAILED = "test_failed"
    COVERAGE_FAILED = "coverage_failed"
    # Fail-closed bucket: a cancelled / timed-out / skipped / absent product
    # subcheck can never be a pass (per reference_ci_gate_enforcement_mechanics).
    PRODUCT_INFRA = "product_infra"


class EnumFreezeSupersedeReason(StrEnum):
    """Why a prior head-freeze is superseded and the candidate returns to BUILD.

    Ordered by detection precedence in ``classify_supersede_reason``. The first
    four are the plan §4 supersession axes; ``TICKET_SET_CHANGE`` covers a
    same-PR change to the occ-preflight-extracted ticket set (a genuine identity
    change that must not silently reuse the old freeze).
    """

    SYNCHRONIZE = "synchronize"  # head SHA moved (new commit pushed)
    BASE_RETARGET = "base_retarget"  # PR base branch changed
    CONTRACT_DIGEST_CHANGE = "contract_digest_change"
    POLICY_DIGEST_CHANGE = "policy_digest_change"  # pinned workflow/policy digest
    TICKET_SET_CHANGE = "ticket_set_change"


def product_outcome_is_freeze_eligible(outcome: EnumProductReadinessOutcome) -> bool:
    """Return True iff a head with this product outcome may be frozen.

    The single freeze-eligibility gate: only an affirmatively ``PRODUCT_GREEN``
    head is eligible. This is the deterministic core of the red-before-OCC
    invariant — a non-green head can never mint head-bound evidence.
    """
    return outcome is EnumProductReadinessOutcome.PRODUCT_GREEN


class ModelFrozenTuple(BaseModel):
    """The minimum idempotency identity of a candidate PR head (plan §4).

    Frozen and ``extra="forbid"``: the tuple is a durable idempotency key, not a
    scratch object. ``freeze_id`` is a pure deterministic fingerprint of the
    identifying fields, so two distinct heads never collide and a replayed tuple
    resolves to the same key.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    repo: str = Field(
        ..., min_length=1, description="Repository name, e.g. omnimarket."
    )
    pr_number: int = Field(..., ge=1, description="GitHub PR number of the lane.")
    ticket_set: tuple[str, ...] = Field(
        ...,
        min_length=1,
        description=(
            "Sorted, de-duplicated set of OMN ticket ids the PR closes/references "
            "(occ-preflight extracts multiple per PR). A single-ticket tuple "
            "silently mis-keys multi-ticket PRs — this is a SET."
        ),
    )
    head_sha: str = Field(
        ..., min_length=7, description="Exact PR head SHA the freeze binds to."
    )
    base_ref: str = Field(
        ...,
        min_length=1,
        description="PR base branch (e.g. dev); a retarget supersedes.",
    )
    contract_digest: str = Field(
        ...,
        min_length=1,
        description="Digest of the central OCC contract bytes at freeze time.",
    )
    policy_digest: str = Field(
        ...,
        min_length=1,
        description=(
            "Pinned workflow/policy digest. Load-bearing: without it a relevant "
            "configuration change cannot be detected against the tuple, so the "
            "supersession trigger would be undetectable (plan §4)."
        ),
    )

    @field_validator("repo", "head_sha", "base_ref", "contract_digest", "policy_digest")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field must contain non-whitespace characters")
        return value

    @field_validator("ticket_set")
    @classmethod
    def _normalize_ticket_set(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = {t.strip() for t in value if t.strip()}
        if not cleaned:
            raise ValueError("ticket_set must contain at least one OMN ticket id")
        for ticket in cleaned:
            if not _TICKET_ID_RE.match(ticket):
                raise ValueError(
                    f"ticket_set entries must match OMN-\\d+, got: {ticket!r}"
                )
        # Sorted so the fingerprint is order-independent (a set, not a list).
        return tuple(sorted(cleaned))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def freeze_id(self) -> str:
        """Deterministic 16-hex fingerprint of the tuple's identity.

        Reuses the canonical ``generate_fingerprint`` primitive. The identity is
        ``(repo#pr_number@base_ref)`` x ``(head_sha:contract:policy:ticket_set)``
        so any change to a supersession axis yields a different key.
        """
        locus = f"{self.repo}#{self.pr_number}@{self.base_ref}"
        symbol = (
            f"{self.head_sha}:{self.contract_digest}:{self.policy_digest}:"
            f"{','.join(self.ticket_set)}"
        )
        return generate_fingerprint(_TUPLE_FINGERPRINT_NAMESPACE, locus, symbol)


def classify_supersede_reason(
    old: ModelFrozenTuple, new: ModelFrozenTuple
) -> EnumFreezeSupersedeReason | None:
    """Classify how ``new`` supersedes ``old`` for the same PR, or ``None``.

    Returns ``None`` when the two tuples are identical (an idempotent replay —
    no supersession). Otherwise returns the highest-precedence changed axis.
    Callers must ensure both tuples share ``(repo, pr_number)``.
    """
    if (old.repo, old.pr_number) != (new.repo, new.pr_number):
        raise ValueError(
            "classify_supersede_reason requires the same (repo, pr_number); "
            f"got {(old.repo, old.pr_number)} vs {(new.repo, new.pr_number)}"
        )
    if old.head_sha != new.head_sha:
        return EnumFreezeSupersedeReason.SYNCHRONIZE
    if old.base_ref != new.base_ref:
        return EnumFreezeSupersedeReason.BASE_RETARGET
    if old.contract_digest != new.contract_digest:
        return EnumFreezeSupersedeReason.CONTRACT_DIGEST_CHANGE
    if old.policy_digest != new.policy_digest:
        return EnumFreezeSupersedeReason.POLICY_DIGEST_CHANGE
    if old.ticket_set != new.ticket_set:
        return EnumFreezeSupersedeReason.TICKET_SET_CHANGE
    return None


class ModelHeadFreezeRecord(BaseModel):
    """A durable freeze record for one ``PRODUCT_GREEN`` head.

    Frozen and ``extra="forbid"``. This is an *idempotency key, not evidence*:
    Governance Readiness re-derives the head SHA and contract digest at
    validation time and never trusts a recorded claim. A freeze record is only
    valid for a ``PRODUCT_GREEN`` outcome — refusing any other outcome is the
    red-before-OCC invariant at the model boundary.

    ``superseder`` is the identity that authored the record; per the
    no-self-authored-evidence rule it must differ from the implementer when the
    record is emitted onto the control-plane surface.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    frozen_tuple: ModelFrozenTuple = Field(
        ..., description="The frozen idempotency identity this record pins."
    )
    product_outcome: EnumProductReadinessOutcome = Field(
        ...,
        description="Product Readiness outcome; must be PRODUCT_GREEN for a freeze.",
    )
    superseder: str = Field(
        ...,
        min_length=1,
        description="Identity that authored the freeze (agent / login / CI).",
    )
    created_at: datetime = Field(
        ..., description="UTC timestamp the freeze was recorded (tz-aware)."
    )

    @field_validator("superseder")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("superseder must contain non-whitespace characters")
        return value

    @field_validator("created_at")
    @classmethod
    def _validate_tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware (UTC)")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _enforce_green(self) -> ModelHeadFreezeRecord:
        if not product_outcome_is_freeze_eligible(self.product_outcome):
            raise ValueError(
                "a head-freeze record is only valid for a PRODUCT_GREEN head; "
                f"got product_outcome={self.product_outcome.value}. Head-bound "
                "OCC evidence must never be minted against an unproven head."
            )
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def freeze_id(self) -> str:
        """The frozen-tuple fingerprint — the record's idempotency key."""
        return self.frozen_tuple.freeze_id


class ModelHeadFreezeSupersession(BaseModel):
    """A net-new record invalidating a prior freeze and routing back to BUILD.

    Frozen and ``extra="forbid"``. Emitted when the frozen tuple moves on the
    same PR (synchronize / base retarget / contract- or policy-digest change /
    ticket-set change). It carries both the superseded key and the new tuple so
    the old evidence can no longer satisfy Governance Readiness and the
    candidate re-enters ``BUILD``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    superseded_freeze_id: str = Field(
        ..., min_length=1, description="freeze_id of the freeze being invalidated."
    )
    superseded_tuple: ModelFrozenTuple = Field(
        ..., description="The tuple whose freeze is superseded."
    )
    new_tuple: ModelFrozenTuple = Field(
        ..., description="The tuple that replaces it (candidate returns to BUILD)."
    )
    reason: EnumFreezeSupersedeReason = Field(
        ..., description="Which identity axis changed."
    )
    superseder: str = Field(
        ..., min_length=1, description="Identity that authored the supersession."
    )
    created_at: datetime = Field(
        ..., description="UTC timestamp the supersession was recorded (tz-aware)."
    )

    @field_validator("superseder", "superseded_freeze_id")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field must contain non-whitespace characters")
        return value

    @field_validator("created_at")
    @classmethod
    def _validate_tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware (UTC)")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _validate_supersession(self) -> ModelHeadFreezeSupersession:
        if self.superseded_freeze_id != self.superseded_tuple.freeze_id:
            raise ValueError(
                "superseded_freeze_id must equal superseded_tuple.freeze_id"
            )
        detected = classify_supersede_reason(self.superseded_tuple, self.new_tuple)
        if detected is None:
            raise ValueError(
                "a supersession requires the new tuple to differ from the "
                "superseded tuple in at least one identity axis"
            )
        if detected is not self.reason:
            raise ValueError(
                f"declared supersession reason {self.reason.value} does not match "
                f"the highest-precedence changed axis {detected.value}"
            )
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def supersession_id(self) -> str:
        """Deterministic fingerprint of this supersession event."""
        locus = f"{self.superseded_freeze_id}->{self.new_tuple.freeze_id}"
        return generate_fingerprint(
            _SUPERSESSION_FINGERPRINT_NAMESPACE, locus, self.reason.value
        )


class FreezeLedger:
    """Deterministic, idempotent in-memory freeze store (local/test surface).

    Proves the three WS1 invariants without any external I/O:

    * **One freeze per green head.** ``freeze`` refuses a non-green outcome and
      dedupes by ``freeze_id`` — replaying an identical tuple returns the
      existing record and never appends a second.
    * **Supersession on tuple change.** When the active freeze for a PR is
      re-frozen with a moved tuple, ``freeze`` emits a
      ``ModelHeadFreezeSupersession`` (marking the old ``freeze_id`` superseded)
      before minting the new record, so stale evidence can no longer satisfy
      Governance Readiness.

    The live enforcement PR swaps this for an append-only control-plane surface;
    this class is the seam that surface must satisfy.
    """

    def __init__(self) -> None:
        self._active: dict[tuple[str, int], ModelHeadFreezeRecord] = {}
        self._records: dict[str, ModelHeadFreezeRecord] = {}
        self._supersessions: list[ModelHeadFreezeSupersession] = []
        self._superseded_ids: set[str] = set()

    def freeze(
        self,
        frozen_tuple: ModelFrozenTuple,
        *,
        product_outcome: EnumProductReadinessOutcome,
        superseder: str,
        created_at: datetime,
    ) -> ModelHeadFreezeRecord:
        """Freeze a ``PRODUCT_GREEN`` head; idempotent, supersession-aware.

        Raises ``ValueError`` if the head is not freeze-eligible (red-before-OCC).
        """
        if not product_outcome_is_freeze_eligible(product_outcome):
            raise ValueError(
                "refusing to freeze a head that is not PRODUCT_GREEN "
                f"(outcome={product_outcome.value}); no head-bound OCC evidence "
                "may be considered valid for an unproven product head."
            )

        key = (frozen_tuple.repo, frozen_tuple.pr_number)
        active = self._active.get(key)
        if active is not None:
            reason = classify_supersede_reason(active.frozen_tuple, frozen_tuple)
            if reason is None:
                # Identical tuple → idempotent replay: no new record, no
                # supersession.
                return active
            # Tuple moved → supersede the old freeze and route back to BUILD.
            self._supersessions.append(
                ModelHeadFreezeSupersession(
                    superseded_freeze_id=active.freeze_id,
                    superseded_tuple=active.frozen_tuple,
                    new_tuple=frozen_tuple,
                    reason=reason,
                    superseder=superseder,
                    created_at=created_at,
                )
            )
            self._superseded_ids.add(active.freeze_id)

        freeze_id = frozen_tuple.freeze_id
        existing = self._records.get(freeze_id)
        if existing is not None:
            # Replay of a previously-seen key (e.g. a head reverted to an earlier
            # frozen SHA) — reuse the original record; still exactly one per key.
            self._active[key] = existing
            self._superseded_ids.discard(freeze_id)
            return existing

        record = ModelHeadFreezeRecord(
            frozen_tuple=frozen_tuple,
            product_outcome=product_outcome,
            superseder=superseder,
            created_at=created_at,
        )
        self._records[freeze_id] = record
        self._active[key] = record
        return record

    def active_for(self, repo: str, pr_number: int) -> ModelHeadFreezeRecord | None:
        """Return the current active freeze for a PR, or ``None``."""
        return self._active.get((repo, pr_number))

    def is_superseded(self, freeze_id: str) -> bool:
        """True if ``freeze_id`` has been superseded (evidence bound to it is stale)."""
        return freeze_id in self._superseded_ids

    def records(self) -> tuple[ModelHeadFreezeRecord, ...]:
        """All unique freeze records, sorted by freeze_id (deterministic)."""
        return tuple(sorted(self._records.values(), key=lambda r: r.freeze_id))

    def supersessions(self) -> tuple[ModelHeadFreezeSupersession, ...]:
        """All emitted supersession records, in emission order."""
        return tuple(self._supersessions)


__all__ = [
    "EnumFreezeSupersedeReason",
    "EnumProductReadinessOutcome",
    "FreezeLedger",
    "ModelFrozenTuple",
    "ModelHeadFreezeRecord",
    "ModelHeadFreezeSupersession",
    "classify_supersede_reason",
    "product_outcome_is_freeze_eligible",
]
