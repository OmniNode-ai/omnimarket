# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Write verbs of the iLO 4 Redfish effect: power, virtual media, boot once (OMN-21835).

A write is planned from state already read (``plan_*`` raise a refusal or return a plan), then run by
:func:`run_plan`, which owns the order that makes a retry safe: journal ``intent`` first, then send, then
journal ``sent``, then read the state back. A plan whose state check finds the server already in the
requested state sends nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue

from omnimarket.nodes.node_ilo_redfish_effect.handlers.protocols import (
    Clock,
    ProtocolActionJournal,
    RedfishTransportError,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.redaction import redact_text
from omnimarket.nodes.node_ilo_redfish_effect.handlers.redfish_client import (
    IloRefusalError,
    JsonObject,
    ModelIloEndpoints,
    RedfishSession,
    api_path,
    as_list,
    as_object,
    link_of,
    message_id,
    odata_ids,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.safety import check_media_url
from omnimarket.nodes.node_ilo_redfish_effect.models.enum_ilo_redfish import (
    EnumIloBootTarget,
    EnumIloJournalStatus,
    EnumIloMediaAction,
    EnumIloOutcome,
    EnumIloPowerAction,
    EnumIloRefusal,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_overlay import (
    ModelIloHost,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_redfish_request import (
    ModelIloRedfishRequest,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_redfish_result import (
    ModelIloEvidenceStep,
    ModelIloJournalRecord,
)

RESET_TYPES: dict[EnumIloPowerAction, str] = {
    EnumIloPowerAction.ON: "On",
    EnumIloPowerAction.FORCE_OFF: "ForceOff",
    EnumIloPowerAction.FORCE_RESTART: "ForceRestart",
    EnumIloPowerAction.POWER_BUTTON: "PushPowerButton",
}
BOOT_VALUES: dict[EnumIloBootTarget, str] = {
    EnumIloBootTarget.PXE: "Pxe",
    EnumIloBootTarget.CD: "Cd",
    EnumIloBootTarget.HDD: "Hdd",
    EnumIloBootTarget.USB: "Usb",
    EnumIloBootTarget.BIOS_SETUP: "BiosSetup",
    EnumIloBootTarget.UEFI_SHELL: "UefiShell",
}
_EXPECTED_POWER: dict[EnumIloPowerAction, str | None] = {
    EnumIloPowerAction.ON: "On",
    EnumIloPowerAction.FORCE_OFF: "Off",
    EnumIloPowerAction.FORCE_RESTART: "On",
    EnumIloPowerAction.POWER_BUTTON: None,
}


class PlannedCall(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    method: Literal["POST", "PATCH"]
    path: str
    body: JsonValue


class WritePlan:
    """What a write will send, what it found, and how to confirm it afterwards."""

    def __init__(
        self,
        calls: list[PlannedCall],
        verify: Callable[[], tuple[bool, str]],
        *,
        noop: str | None = None,
        found: str = "",
    ) -> None:
        self.calls = calls
        self.verify = verify
        self.noop = noop
        self.found = found


# -- planning ------------------------------------------------------------------------------


def plan_power(
    session: RedfishSession,
    endpoints: ModelIloEndpoints,
    system: JsonObject,
    request: ModelIloRedfishRequest,
) -> WritePlan:
    action = request.power_action
    if action is None:  # the request model guarantees it; keep the narrowing explicit
        raise IloRefusalError(EnumIloRefusal.UNSUPPORTED_VALUE, "no power action")
    state = str(system.get("PowerState") or "")
    reset = as_object(as_object(system.get("Actions")).get("#ComputerSystem.Reset"))
    target = reset.get("target")
    target_path = api_path(target) if isinstance(target, str) else None
    if target_path is None:
        raise IloRefusalError(
            EnumIloRefusal.ENDPOINT_MISSING, "Reset action not advertised"
        )
    reset_type = RESET_TYPES[action]
    allowed = reset.get("ResetType@Redfish.AllowableValues")
    if isinstance(allowed, list) and reset_type not in allowed:
        raise IloRefusalError(
            EnumIloRefusal.UNSUPPORTED_VALUE, f"{reset_type} not allowed here"
        )
    expected = _EXPECTED_POWER[action]

    def verify() -> tuple[bool, str]:
        observed = str(session.get(endpoints.system).get("PowerState") or "")
        if expected is None:
            return (
                True,
                f"PowerState={observed}; a power-button press is not confirmable by state",
            )
        note = ""
        if action is EnumIloPowerAction.FORCE_RESTART:
            note = "; a restart is confirmed only by the system reporting On again"
        return observed == expected, f"PowerState={observed}{note}"

    found = f"PowerState={state}"
    if action is EnumIloPowerAction.ON and state == "On":
        return WritePlan([], verify, noop="already On", found=found)
    if action is EnumIloPowerAction.FORCE_OFF and state == "Off":
        return WritePlan([], verify, noop="already Off", found=found)
    if action is EnumIloPowerAction.FORCE_RESTART and state != "On":
        raise IloRefusalError(
            EnumIloRefusal.PRECONDITION_FAILED,
            f"restart needs PowerState On, found {state}",
        )
    return WritePlan(
        [PlannedCall(method="POST", path=target_path, body={"ResetType": reset_type})],
        verify,
        found=found,
    )


def _cd_member(session: RedfishSession, manager: JsonObject) -> tuple[str, JsonObject]:
    collection = link_of(manager, "VirtualMedia")
    if collection is None:
        raise IloRefusalError(
            EnumIloRefusal.ENDPOINT_MISSING, "VirtualMedia not advertised"
        )
    found: list[tuple[str, JsonObject]] = []
    for path in odata_ids(session.get(collection)):
        body = session.get(path)
        kinds = {str(t).lower() for t in as_list(body.get("MediaTypes"))}
        if kinds & {"cd", "dvd"}:
            found.append((path, body))
    if not found:
        raise IloRefusalError(
            EnumIloRefusal.ENDPOINT_MISSING, "no CD/DVD virtual media member"
        )
    if len(found) > 1:
        raise IloRefusalError(
            EnumIloRefusal.AMBIGUOUS_ENDPOINT, "several CD/DVD virtual media members"
        )
    return found[0]


def _action_target(member: JsonObject, suffix: str) -> str | None:
    for name, value in as_object(member.get("Actions")).items():
        if name.endswith(suffix):
            target = as_object(value).get("target")
            if isinstance(target, str):
                return api_path(target)
    return None


def plan_virtual_media(
    session: RedfishSession,
    endpoints: ModelIloEndpoints,
    host: ModelIloHost,
    request: ModelIloRedfishRequest,
) -> WritePlan:
    manager = session.get(endpoints.manager)
    member_path, member = _cd_member(session, manager)
    inserted = member.get("Inserted") is True
    image = member.get("Image")
    found = f"Inserted={inserted}"

    if request.media_action is EnumIloMediaAction.INSERT:
        url = request.image_url
        if url is None:
            raise IloRefusalError(EnumIloRefusal.MEDIA_URL_NOT_ALLOWED, "no image_url")
        check_media_url(host, url)

        def verify_insert() -> tuple[bool, str]:
            now = session.get(member_path)
            ok = now.get("Inserted") is True and now.get("Image") == url
            return (
                ok,
                f"Inserted={now.get('Inserted')} image_matches={now.get('Image') == url}",
            )

        if inserted and image == url:
            return WritePlan(
                [], verify_insert, noop="same image already inserted", found=found
            )
        if inserted:
            raise IloRefusalError(
                EnumIloRefusal.PRECONDITION_FAILED,
                "a different image is inserted; eject it first under its own approval",
            )
        target = _action_target(member, "InsertVirtualMedia")
        call = (
            PlannedCall(method="POST", path=target, body={"Image": url})
            if target is not None
            else PlannedCall(method="PATCH", path=member_path, body={"Image": url})
        )
        return WritePlan([call], verify_insert, found=found)

    def verify_eject() -> tuple[bool, str]:
        now = session.get(member_path)
        return now.get("Inserted") is not True, f"Inserted={now.get('Inserted')}"

    if not inserted:
        return WritePlan([], verify_eject, noop="nothing inserted", found=found)
    target = _action_target(member, "EjectVirtualMedia")
    call = (
        PlannedCall(method="POST", path=target, body={})
        if target is not None
        else PlannedCall(method="PATCH", path=member_path, body={"Image": None})
    )
    return WritePlan([call], verify_eject, found=found)


def plan_boot_once(
    session: RedfishSession,
    endpoints: ModelIloEndpoints,
    system: JsonObject,
    request: ModelIloRedfishRequest,
) -> WritePlan:
    target = request.boot_target
    if target is None:
        raise IloRefusalError(EnumIloRefusal.UNSUPPORTED_VALUE, "no boot target")
    value = BOOT_VALUES[target]
    boot = as_object(system.get("Boot"))

    if "BootSourceOverrideTarget" in boot:
        allowed = boot.get("BootSourceOverrideTarget@Redfish.AllowableValues")
        if isinstance(allowed, list) and value not in allowed:
            raise IloRefusalError(
                EnumIloRefusal.UNSUPPORTED_VALUE, f"{value} not allowed here"
            )
        current_target = boot.get("BootSourceOverrideTarget")
        current_enabled = boot.get("BootSourceOverrideEnabled")
        found = f"target={current_target} enabled={current_enabled}"

        def verify_override() -> tuple[bool, str]:
            now = as_object(session.get(endpoints.system).get("Boot"))
            ok = (
                now.get("BootSourceOverrideTarget") == value
                and now.get("BootSourceOverrideEnabled") == "Once"
            )
            return (
                ok,
                f"target={now.get('BootSourceOverrideTarget')} enabled={now.get('BootSourceOverrideEnabled')}",
            )

        if current_target == value and current_enabled == "Once":
            return WritePlan(
                [], verify_override, noop="override already set once", found=found
            )
        calls: list[PlannedCall] = []
        # HPE documents that the target must be specified before the enabled value can be used.
        if current_target != value:
            calls.append(
                PlannedCall(
                    method="PATCH",
                    path=endpoints.system,
                    body={"Boot": {"BootSourceOverrideTarget": value}},
                )
            )
        calls.append(
            PlannedCall(
                method="PATCH",
                path=endpoints.system,
                body={"Boot": {"BootSourceOverrideEnabled": "Once"}},
            )
        )
        return WritePlan(calls, verify_override, found=found)

    if target is EnumIloBootTarget.CD:
        manager = session.get(endpoints.manager)
        member_path, member = _cd_member(session, manager)
        hp = as_object(as_object(member.get("Oem")).get("Hp"))
        if "BootOnNextServerReset" not in hp:
            raise IloRefusalError(
                EnumIloRefusal.ENDPOINT_MISSING, "boot override not advertised"
            )
        if member.get("Inserted") is not True:
            raise IloRefusalError(
                EnumIloRefusal.PRECONDITION_FAILED, "insert the media first"
            )
        found = f"BootOnNextServerReset={hp.get('BootOnNextServerReset')}"

        def verify_flag() -> tuple[bool, str]:
            now = as_object(as_object(session.get(member_path).get("Oem")).get("Hp"))
            return now.get(
                "BootOnNextServerReset"
            ) is True, f"BootOnNextServerReset={now.get('BootOnNextServerReset')}"

        if hp.get("BootOnNextServerReset") is True:
            return WritePlan(
                [], verify_flag, noop="boot-on-next-reset already set", found=found
            )
        return WritePlan(
            [
                PlannedCall(
                    method="PATCH",
                    path=member_path,
                    body={"Oem": {"Hp": {"BootOnNextServerReset": True}}},
                )
            ],
            verify_flag,
            found=found,
        )

    raise IloRefusalError(
        EnumIloRefusal.ENDPOINT_MISSING, "boot override not advertised"
    )


# -- running -------------------------------------------------------------------------------


class RunContext(BaseModel):
    """Everything :func:`run_plan` needs besides the plan and the live session."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    key: str
    action: str


class PlanRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumIloOutcome
    refusal: EnumIloRefusal | None = None
    detail: str | None = None
    evidence: tuple[ModelIloEvidenceStep, ...] = ()


def run_plan(
    plan: WritePlan,
    session: RedfishSession,
    journal: ProtocolActionJournal,
    context: RunContext,
    *,
    clock: Clock,
    wait_for: Callable[[Callable[[], tuple[bool, str]]], tuple[bool, str]],
) -> PlanRun:
    """Send the plan's calls in order, journalling around them, then confirm by reading back."""

    def record(
        status: EnumIloJournalStatus,
        http_status: int | None = None,
        detail: str | None = None,
    ) -> None:
        journal.append(
            ModelIloJournalRecord(
                key=context.key,
                host=context.host,
                action=context.action,
                status=status,
                at=clock(),
                http_status=http_status,
                detail=redact_text(detail) if detail else None,
            )
        )

    evidence: list[ModelIloEvidenceStep] = [
        ModelIloEvidenceStep(step="found", observed=plan.found)
    ]
    if plan.noop is not None:
        evidence.append(ModelIloEvidenceStep(step="noop", observed=plan.noop))
        return PlanRun(outcome=EnumIloOutcome.NOOP, evidence=tuple(evidence))

    record(EnumIloJournalStatus.INTENT, detail=f"{len(plan.calls)} call(s)")
    for index, call in enumerate(plan.calls, start=1):
        first = index == 1
        try:
            response = session.send_write(call.method, call.path, call.body)
        except RedfishTransportError as exc:
            if not exc.request_sent and first:
                record(
                    EnumIloJournalStatus.REJECTED,
                    detail=f"transport {exc.kind} before send",
                )
                refusal = (
                    EnumIloRefusal.TLS_FAILED
                    if exc.kind == "tls"
                    else EnumIloRefusal.CONNECT_FAILED
                )
                evidence.append(
                    ModelIloEvidenceStep(
                        step=f"call {index}", observed=f"not sent ({exc.kind})"
                    )
                )
                return PlanRun(
                    outcome=EnumIloOutcome.FAILED,
                    refusal=refusal,
                    detail=exc.kind,
                    evidence=tuple(evidence),
                )
            record(
                EnumIloJournalStatus.UNKNOWN,
                detail=f"transport {exc.kind} on call {index}",
            )
            evidence.append(
                ModelIloEvidenceStep(
                    step=f"call {index}", observed=f"outcome unknown ({exc.kind})"
                )
            )
            return PlanRun(
                outcome=EnumIloOutcome.UNKNOWN,
                detail=exc.kind,
                evidence=tuple(evidence),
            )
        evidence.append(
            ModelIloEvidenceStep(
                step=f"call {index}", observed=f"http {response.status}"
            )
        )
        if 200 <= response.status < 300:
            record(EnumIloJournalStatus.SENT, response.status, f"call {index}")
            continue
        code = message_id(response.body)
        if 400 <= response.status < 500:
            if first:
                record(EnumIloJournalStatus.REJECTED, response.status, code)
                refusal = (
                    EnumIloRefusal.INSUFFICIENT_PRIVILEGE
                    if response.status == 403
                    else EnumIloRefusal.AUTH_FAILED
                    if response.status == 401
                    else EnumIloRefusal.DEVICE_ERROR
                )
                return PlanRun(
                    outcome=EnumIloOutcome.FAILED,
                    refusal=refusal,
                    detail=code,
                    evidence=tuple(evidence),
                )
            record(
                EnumIloJournalStatus.UNKNOWN,
                response.status,
                f"call {index} rejected after an earlier call was sent",
            )
            return PlanRun(
                outcome=EnumIloOutcome.UNKNOWN,
                detail="a later call was rejected after an earlier one was sent",
                evidence=tuple(evidence),
            )
        record(
            EnumIloJournalStatus.UNKNOWN, response.status, f"call {index} server error"
        )
        return PlanRun(
            outcome=EnumIloOutcome.UNKNOWN,
            detail=f"http {response.status}",
            evidence=tuple(evidence),
        )

    confirmed, observed = wait_for(plan.verify)
    evidence.append(ModelIloEvidenceStep(step="verify", observed=redact_text(observed)))
    if confirmed:
        record(EnumIloJournalStatus.VERIFIED, detail=observed)
        return PlanRun(outcome=EnumIloOutcome.OK, evidence=tuple(evidence))
    record(EnumIloJournalStatus.UNVERIFIED, detail=observed)
    return PlanRun(outcome=EnumIloOutcome.UNVERIFIED, evidence=tuple(evidence))
