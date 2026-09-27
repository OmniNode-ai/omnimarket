# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed declaration and receipt for the house routing-overlay writer (OMN-19186).

The declaration is the whole of AC3's "refuses a partial one". Refusal lives
in the MODEL rather than in the handler on purpose: a partial declaration must
be un-representable, not merely rejected by whichever caller remembered to
check. ``extra="forbid"`` plus required fields plus non-empty validation means
there is no construction path that produces a half-bound rung, including the
ones nobody has written yet.

What a partial declaration would cost, concretely: the routing reducer refuses
an overlay row with no declared ``provider`` provenance at ROUTING time
(``handler_delegation_routing._decision_from_tenant_overlay``), so a row
written without one is a stored landmine that fails the next delegation rather
than the write that created it. The declaration therefore requires every field
the reducer will later demand.

There is deliberately NO tenant field. This writer writes house rows and only
house rows -- ``tenant_id`` is the constant, not an argument -- so it cannot be
pointed at a customer's binding by a caller, a typo, or a future refactor. The
BYOK bridge owns the customer arm and this node must never grow into it.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EnumHouseOverlayOperation(StrEnum):
    """What the writer is being asked to do to the house's own binding."""

    DECLARE = "declare"
    RETIRE = "retire"


def _require_clean(value: str, *, field: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field} must not be blank")
    if stripped != value:
        raise ValueError(f"{field} must not carry leading or trailing whitespace")
    return stripped


class ModelHouseRoutingOverlayDeclaration(BaseModel):
    """One lab inference rung, declared in full.

    ``task_types`` is this rung's TIER MEMBERSHIP as the v1(a) table can
    express it: an overlay row short-circuits tier iteration for one task type,
    so the set of task types a rung serves is the set of rows it gets.
    ``tier_name`` names the tier the rung belongs to and is validated by the
    handler against the deployed routing-tiers contract -- v1(a) keeps routing
    STRUCTURE platform-fixed (migration 0001's header), so this field bounds a
    declaration to an existing tier rather than inventing one.

    The honest limit, stated here rather than left to be discovered: the v1(a)
    table carries no tier column, so ``tier_name`` is enforced at write time
    and recorded on the receipt, not persisted beside the row. Making tier
    membership durable is a migration, and it is not this ticket.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str = Field(..., min_length=1)
    endpoint_url: str = Field(..., min_length=1)
    model_name: str = Field(..., min_length=1)
    provider: str = Field(..., min_length=1)
    tier_name: str = Field(..., min_length=1)
    task_types: tuple[str, ...] = Field(..., min_length=1)
    secret_ref: str | None = None
    timeout_ms: int | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)

    @field_validator("backend_id", "model_name", "provider", "tier_name")
    @classmethod
    def _clean_required(cls, value: str, info: object) -> str:
        name = getattr(info, "field_name", "field")
        return _require_clean(value, field=str(name))

    @field_validator("secret_ref")
    @classmethod
    def _clean_optional_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # A blank ref is NOT the same as an absent one to the effect boundary,
        # and the difference has already cost one live outage on the customer
        # arm (OMN-18191). Refuse it at the door instead of storing the
        # ambiguity.
        return _require_clean(value, field="secret_ref")

    @field_validator("endpoint_url")
    @classmethod
    def _endpoint_is_an_address_not_a_promise(cls, value: str) -> str:
        cleaned = _require_clean(value, field="endpoint_url")
        if "://" not in cleaned:
            raise ValueError(
                "endpoint_url must carry a scheme, e.g. "
                "http://host:8000/v1/chat/completions"
            )
        scheme, _, remainder = cleaned.partition("://")
        if scheme not in {"http", "https"}:
            raise ValueError(
                f"endpoint_url scheme must be http or https, got {scheme!r}"
            )
        if not remainder.strip():
            raise ValueError("endpoint_url must carry a host")
        # SHAPE only. Reachability is deliberately NOT checked here: a rung
        # whose host is down is a health fact and must stay declarable, so
        # that the health surface reports it rather than the write refusing it
        # (AC4). A writer that probed would make "the lab box is rebooting"
        # indistinguishable from "this declaration is malformed".
        return cleaned

    @field_validator("task_types")
    @classmethod
    def _task_types_are_named_not_wildcarded(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        cleaned = tuple(
            _require_clean(item, field="task_types entry") for item in value
        )
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("task_types must not repeat a task type")
        if "*" in cleaned:
            # ``*`` is the BYOK sentinel: one row binding EVERY task type of a
            # tenant to one backend, which is the right shape for "the
            # customer registered a key" and the wrong shape for "we added a
            # GPU". A house wildcard would silently take every task class off
            # the platform ladder in one write, and nothing downstream would
            # report that as anything but a successful registration.
            raise ValueError(
                "task_types must name task types explicitly; the '*' sentinel is "
                "the BYOK all-task-types shape and is not available to a house "
                "rung declaration"
            )
        return cleaned


class ModelHouseRoutingOverlayCommand(BaseModel):
    """Declare or retire one house rung.

    A retire names the rung and the task types to withdraw and nothing else:
    restating an endpoint in order to remove it invites a caller to retire
    something other than what they think they are retiring.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumHouseOverlayOperation
    declaration: ModelHouseRoutingOverlayDeclaration | None = None
    retire_backend_id: str | None = None
    retire_task_types: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _payload_matches_operation(self) -> ModelHouseRoutingOverlayCommand:
        if self.operation is EnumHouseOverlayOperation.DECLARE:
            if self.declaration is None:
                raise ValueError("a declare command requires a declaration")
            if self.retire_backend_id is not None or self.retire_task_types:
                raise ValueError("a declare command must not carry retire fields")
            return self
        if self.declaration is not None:
            raise ValueError("a retire command must not carry a declaration")
        if not self.retire_backend_id or not self.retire_backend_id.strip():
            raise ValueError("a retire command requires retire_backend_id")
        if not self.retire_task_types:
            raise ValueError(
                "a retire command requires retire_task_types; an unbounded "
                "retire would withdraw every rung this backend serves without "
                "the caller having said so"
            )
        return self


class ModelHouseRoutingOverlayReceipt(BaseModel):
    """What the writer did, in terms a reader can falsify.

    ``rows_written`` / ``rows_removed`` are counts the STORE reported, not the
    count the handler intended -- a retire that matched nothing reports 0
    rather than success, which is the difference between "withdrawn" and
    "was never there".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumHouseOverlayOperation
    tenant_id: str
    backend_id: str
    tier_name: str | None = None
    task_types: tuple[str, ...]
    rows_written: int = 0
    rows_removed: int = 0
    endpoint_url: str | None = None
    model_name: str | None = None
    secret_ref_declared: bool = False
    #: Always False, and asserted to be. The field exists so that AC4 is
    #: readable off a receipt rather than inferred from the absence of a probe.
    endpoint_reachability_probed: bool = False


__all__ = [
    "EnumHouseOverlayOperation",
    "ModelHouseRoutingOverlayCommand",
    "ModelHouseRoutingOverlayDeclaration",
    "ModelHouseRoutingOverlayReceipt",
]
