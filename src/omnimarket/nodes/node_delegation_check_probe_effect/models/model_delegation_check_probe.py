# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the delegation check probe node (OMN-26043): request, observations, run record and overlay.

The run record is shaped as the MVP launch block's ``ModelMvpRun`` (run_id, created_at, event, status, conclusion). Only a
PASS is ``success``. A FAIL, a NOT_MEASURED (the local tier never answered, so no clause was proven or refuted), a
PRECONDITION-ABSENT (an instrument or collector was absent, nothing graded) and an ERROR (the probe itself broke) all
conclude ``failure``: neither is evidence of green.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.routing.delegation_check_responder_chain import (
    ModelResponderAttempt,
)
from omnimarket.models.overlay_documents.model_delegation_check_probe_overlay import (
    CheckId,
    ModelDelegationCheckProbeOverlay,
)

__all__ = ["CheckId", "ModelDelegationCheckProbeOverlay"]

CAUSE_PRODUCT = "product"
CAUSE_LAB_MODEL = "lab-model"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumCheckVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_MEASURED = "NOT_MEASURED"
    PRECONDITION_ABSENT = "PRECONDITION-ABSENT"
    ERROR = "ERROR"


class ModelCheckClause(_Frozen):
    name: str = Field(min_length=1)
    ok: bool
    detail: str


class ModelCheckPrecondition(_Frozen):
    name: str = Field(min_length=1)
    present: bool
    detail: str


class ModelDelegateObservation(_Frozen):
    """What one real ``onex delegate`` run on the customer machine showed, read from outside the process (C13 and C14)."""

    rc: int
    # The install is from the package index: the CLI's runtime identity says source=registry, no checkout above it.
    no_checkout: bool = False
    # result.txt, receipt.json and run.json exist, parse, and agree on the correlation and run ids.
    three_files_agree: bool = False
    receipt_tier: str | None = None
    receipt_model: str | None = None
    receipt_endpoint: str | None = None
    # The tier of each attempt on the receipt's ladder, in order; the last is the accepted rung when ``accepted_last``.
    attempt_tiers: tuple[str, ...] = ()
    accepted_last: bool = False
    escalated: bool = False
    cost_usd: float = 0.0
    # Hosts the session connected to other than loopback and the chosen responder; ``egress_recorded`` is False when the
    # instrument (strace or the recording proxy) was absent or blind.
    egress_recorded: bool = False
    egress_external: tuple[str, ...] = ()
    # The responder's own connect and token counter moved: the instrument saw the process that did the work.
    model_connects: int = 0
    token_counter_moved: bool = False
    # A deliberate outbound connect, through the same wrapper and parser, was classified external.
    control_external_classified: bool = False
    # The negative control: a run before the customer's overlay file exists was refused and never reached the model server, so
    # the configured model is proven to come from the configuration. None means the collector did not run it.
    negative_control_refused: bool | None = None
    # Where the customer machine resolves ``onex`` after the install, and whether that is the install's own packaged entry. A
    # customer install ships ``onex`` on PATH; a runner that carries its own ``onex`` (runner image v15) must not stand in for
    # it, so what is graded is that the delegate ran through the install's entry, never that the runner has no ``onex``.
    entry_path: str | None = None
    entry_is_install_own: bool = False
    # The ``onex`` the runner itself resolves, recorded beside the run; it is not graded.
    runner_entry_path: str | None = None


class ModelRoutingCollection(_Frozen):
    """What the routing walk of C14 collected on the installed product (rows 2 and 4)."""

    unbindable: tuple[str, ...] = ()
    undeclared_control_classified_deleted: bool = False
    closed_port_control_classified_unreachable: bool = False
    judge_endpoint_private: bool = False
    judge_needs_credential: bool = True
    judge_serves_model: bool = False


class ModelBrokenCommandCase(_Frozen):
    entry_point: str = Field(min_length=1)
    rc: int
    output: str


class ModelSurfaceObservation(_Frozen):
    """What the clean-machine run of C17 showed."""

    guides_lead_with_local: bool = False
    install_rc: int | None = None
    install_interpreter_matches: bool = False
    help_answers: bool = False
    parameter_findings: tuple[str, ...] = ()
    parameter_controls_caught: bool = False
    # The environment switch the installed core names for showing every command group, read from the installed core. The
    # customer surface hides undeclared groups, so a planted broken entry point answers "No such command" unless it runs
    # with this switch on; None means the installed core was not read.
    operator_switch_env: str | None = None
    broken_ran_with_switch: bool = False
    broken_before_rc: int | None = None
    broken_after_rc: int | None = None
    broken_cases: tuple[ModelBrokenCommandCase, ...] = ()


class ModelDelegationCheckRequest(_Frozen):
    correlation_id: str = Field(min_length=1)
    check_id: CheckId
    # ``schedule`` is what the timer unit sends.
    trigger: Literal["schedule", "manual"] = "schedule"


class ModelDelegationCheckReading(_Frozen):
    """What one check graded: the clauses, the surfaces it needed and the responder chain it used. Never a credential value."""

    clauses: tuple[ModelCheckClause, ...] = ()
    preconditions: tuple[ModelCheckPrecondition, ...] = ()
    chain: tuple[ModelResponderAttempt, ...] = ()
    chosen_tier: str | None = None
    chosen_responder: str | None = None
    served_model: str | None = None
    window: int | None = None
    # NOT_MEASURED: the task never reached a local responder that answered.
    not_measured: str | None = None
    versions: dict[str, str] = Field(default_factory=dict)
    # A gateway-graded check names the gateway address it talked to and the tenant it acted as (OMN-26642).
    endpoint: str | None = None
    tenant_slug: str | None = None

    def verdict(self) -> EnumCheckVerdict:
        if any(not p.present for p in self.preconditions):
            return EnumCheckVerdict.PRECONDITION_ABSENT
        if self.not_measured is not None:
            return EnumCheckVerdict.NOT_MEASURED
        if not self.clauses:
            return EnumCheckVerdict.ERROR
        return (
            EnumCheckVerdict.PASS
            if all(c.ok for c in self.clauses)
            else EnumCheckVerdict.FAIL
        )


class ModelDelegationCheckResult(_Frozen):
    """One run of one check, as emitted on the bus and appended to the run history."""

    correlation_id: str
    check_id: CheckId
    run_id: str = Field(min_length=1)
    created_at: datetime
    finished_at: datetime
    event: Literal["schedule", "manual"]
    status: Literal["completed", "errored"]
    conclusion: Literal["success", "failure"]
    verdict: EnumCheckVerdict
    cause_class: Literal["product", "lab-model"] | None = None
    clauses: tuple[ModelCheckClause, ...] = ()
    preconditions: tuple[ModelCheckPrecondition, ...] = ()
    chain: tuple[ModelResponderAttempt, ...] = ()
    chosen_tier: str | None = None
    chosen_responder: str | None = None
    served_model: str | None = None
    window: int | None = None
    versions: dict[str, str] = Field(default_factory=dict)
    error: str | None = None
    emitted: bool = False
    # The gateway address and test tenant of a gateway-graded check; the runner is always this node, never a workflow.
    gateway_url: str | None = None
    tenant_slug: str | None = None
    runner: str = "omnimarket-node"

    @model_validator(mode="after")
    def _aware_and_consistent(self) -> ModelDelegationCheckResult:
        if self.created_at.tzinfo is None or self.finished_at.tzinfo is None:
            raise ValueError("created_at and finished_at carry a time zone")
        if (self.conclusion == "success") != (self.verdict is EnumCheckVerdict.PASS):
            raise ValueError("only a PASS concludes success")
        if (self.status == "errored") != (self.verdict is EnumCheckVerdict.ERROR):
            raise ValueError("status errored if and only if the verdict is ERROR")
        return self

    def mvp_run(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "created_at": self.created_at.isoformat(),
            "event": self.event,
            "status": self.status,
            "conclusion": self.conclusion,
            "versions": dict(self.versions),
        }
