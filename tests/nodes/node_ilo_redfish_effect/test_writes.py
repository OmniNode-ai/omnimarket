"""Write verbs: refusal, identity, retry safety and verification (OMN-21835). Mocked iLO 4, no network."""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnimarket.nodes.node_ilo_redfish_effect.models.enum_ilo_redfish import (
    EnumIloBootTarget,
    EnumIloJournalStatus,
    EnumIloMediaAction,
    EnumIloOutcome,
    EnumIloPowerAction,
    EnumIloRefusal,
    EnumIloVerb,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_redfish_request import (
    ModelIloRedfishRequest,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_redfish_result import (
    ModelIloApproval,
)
from tests.nodes.node_ilo_redfish_effect.conftest import (
    APPROVAL_REF,
    IMAGE_URL,
    OPERATOR_LANE,
    Harness,
    build,
    consent,
    scope_for,
)
from tests.nodes.node_ilo_redfish_effect.ilo_fakes import (
    CD_MEMBER,
    LICENSE_KEY,
    SYSTEM,
    TOKEN,
    put,
)

pytestmark = pytest.mark.unit


def power(
    host: str, action: EnumIloPowerAction, *, ref: str | None = APPROVAL_REF
) -> ModelIloRedfishRequest:
    return ModelIloRedfishRequest(
        verb=EnumIloVerb.POWER,
        host=host,
        requested_by=OPERATOR_LANE,
        approval_ref=ref,
        power_action=action,
    )


def media(
    host: str,
    action: EnumIloMediaAction,
    url: str | None = None,
    *,
    ref: str | None = APPROVAL_REF,
) -> ModelIloRedfishRequest:
    return ModelIloRedfishRequest(
        verb=EnumIloVerb.VIRTUAL_MEDIA,
        host=host,
        requested_by=OPERATOR_LANE,
        approval_ref=ref,
        media_action=action,
        image_url=url,
    )


def boot(
    host: str, target: EnumIloBootTarget, *, ref: str | None = APPROVAL_REF
) -> ModelIloRedfishRequest:
    return ModelIloRedfishRequest(
        verb=EnumIloVerb.BOOT_ONCE,
        host=host,
        requested_by=OPERATOR_LANE,
        approval_ref=ref,
        boot_target=target,
    )


def approved(host: str, action: str, image: str | None = None) -> Harness:
    gen = "gen8" if host == "h8" else "gen9"
    return build(gen, approvals=(consent(scope_for(host, action, image)),))


# -- refused writes --------------------------------------------------------------------------


def test_refused_write_without_an_approval_ref_sends_nothing_and_opens_no_connection() -> (
    None
):
    h = build("gen9")
    result = h.handler.handle(power("h9", EnumIloPowerAction.FORCE_RESTART, ref=None))
    assert result.outcome is EnumIloOutcome.REFUSED
    assert result.refusal is EnumIloRefusal.APPROVAL_MISSING
    assert h.factory_calls == []
    assert h.device.calls == []
    assert h.credentials.asked == []
    assert h.journal.records == []
    assert result.ledger_row is not None
    assert "outcome=refused" in result.ledger_row


def test_refused_write_for_a_consent_row_that_does_not_exist() -> None:
    h = build("gen9")
    result = h.handler.handle(power("h9", EnumIloPowerAction.FORCE_RESTART))
    assert result.refusal is EnumIloRefusal.APPROVAL_INVALID
    assert h.device.calls == []


RESTART_SCOPE = "ilo-redfish host=h9 action=power:force_restart"


@pytest.mark.parametrize(
    ("approval", "why"),
    [
        (consent("ilo-redfish host=h9 action=power:on"), "scope lacks action"),
        (consent("ilo-redfish host=h8 action=power:force_restart"), "scope lacks host"),
        (consent("host=h9 action=power:force_restart"), "scope lacks ilo-redfish"),
        (consent(RESTART_SCOPE, approved_by="stranger"), "approver not allowed"),
        (consent(RESTART_SCOPE, lane=OPERATOR_LANE), "self-approval"),
        (consent(RESTART_SCOPE, age=timedelta(hours=25)), "approval expired"),
        (consent(RESTART_SCOPE, age=timedelta(hours=-1)), "dated in the future"),
    ],
)
def test_refused_write_when_the_consent_row_does_not_cover_the_request(
    approval: ModelIloApproval, why: str
) -> None:
    h = build("gen9", approvals=(approval,))
    result = h.handler.handle(power("h9", EnumIloPowerAction.FORCE_RESTART))
    assert result.outcome is EnumIloOutcome.REFUSED
    assert result.refusal is EnumIloRefusal.APPROVAL_INVALID
    assert why in (result.detail or "")
    assert h.device.calls == []
    assert h.factory_calls == []


def test_refused_write_for_virtual_media_when_the_approval_names_another_image() -> (
    None
):
    h = approved("h9", "virtual_media:insert", "http://192.0.2.10/iso/other.iso")
    result = h.handler.handle(media("h9", EnumIloMediaAction.INSERT, IMAGE_URL))
    assert result.refusal is EnumIloRefusal.APPROVAL_INVALID
    assert h.device.write_calls() == []


def test_refused_write_by_a_read_only_host_row_that_is_not_live() -> None:
    h = build("gen9", live=False, approvals=(consent(scope_for("h9", "power:on")),))
    result = h.handler.handle(power("h9", EnumIloPowerAction.ON))
    assert result.refusal is EnumIloRefusal.HOST_NOT_LIVE
    assert h.device.calls == []


def test_refused_write_without_a_write_credential_never_falls_back_to_the_read_account() -> (
    None
):
    h = build(
        "gen9",
        approvals=(consent(scope_for("h9", "power:force_restart")),),
        credentials={"ILO_H9_READ_USERNAME": "reader", "ILO_H9_READ_PASSWORD": "x"},
    )
    result = h.handler.handle(power("h9", EnumIloPowerAction.FORCE_RESTART))
    assert result.refusal is EnumIloRefusal.CREDENTIAL_MISSING
    assert h.factory_calls == []


def test_a_write_uses_the_write_account_and_a_read_uses_the_read_account() -> None:
    h = approved("h9", "power:force_restart")
    h.handler.handle(power("h9", EnumIloPowerAction.FORCE_RESTART))
    assert h.credentials.asked == ["ILO_H9_WRITE_USERNAME", "ILO_H9_WRITE_PASSWORD"]


# -- identity --------------------------------------------------------------------------------


def test_identity_mismatch_on_the_serial_sends_no_write() -> None:
    h = approved("h9", "power:force_restart")
    h.device.resources[SYSTEM]["SerialNumber"] = "NOTTHESERVER"
    result = h.handler.handle(power("h9", EnumIloPowerAction.FORCE_RESTART))
    assert result.refusal is EnumIloRefusal.IDENTITY_MISMATCH
    assert h.device.write_calls() == []
    assert h.journal.records == []
    assert "NOTTHESERVER" not in (result.detail or "")


def test_identity_mismatch_on_the_product_sends_no_write() -> None:
    h = approved("h9", "power:force_restart")
    h.device.resources[SYSTEM]["Model"] = "ProLiant DL580 Gen9"
    result = h.handler.handle(power("h9", EnumIloPowerAction.FORCE_RESTART))
    assert result.refusal is EnumIloRefusal.IDENTITY_MISMATCH
    assert h.device.write_calls() == []


def test_identity_mismatch_when_the_system_reports_the_other_generation() -> None:
    h = approved("h9", "power:force_restart")
    h.device.resources[SYSTEM]["Model"] = "ProLiant DL360 Gen8"
    result = h.handler.handle(power("h9", EnumIloPowerAction.FORCE_RESTART))
    assert result.refusal is EnumIloRefusal.IDENTITY_MISMATCH


def test_identity_unconfirmed_when_the_overlay_has_no_expected_serial() -> None:
    h = build(
        "gen9",
        serial=None,
        approvals=(consent(scope_for("h9", "power:force_restart")),),
    )
    result = h.handler.handle(power("h9", EnumIloPowerAction.FORCE_RESTART))
    assert result.refusal is EnumIloRefusal.IDENTITY_UNCONFIRMED
    assert h.device.write_calls() == []


# -- power -----------------------------------------------------------------------------------


def test_force_restart_sends_one_reset_records_intent_before_sent_and_verifies() -> (
    None
):
    h = approved("h9", "power:force_restart")
    result = h.handler.handle(power("h9", EnumIloPowerAction.FORCE_RESTART))
    assert result.outcome is EnumIloOutcome.OK
    assert h.device.write_calls() == [
        (
            "POST",
            "/redfish/v1/systems/1/actions/computersystem.reset/",
            {"ResetType": "ForceRestart"},
        )
    ]
    statuses = [r.status for r in h.journal.records]
    assert statuses == [
        EnumIloJournalStatus.INTENT,
        EnumIloJournalStatus.SENT,
        EnumIloJournalStatus.VERIFIED,
    ]
    assert any(
        e.step == "verify" and "PowerState=On" in e.observed for e in result.evidence
    )
    assert result.ledger_row is not None
    assert "action" not in result.ledger_row.split("|")
    assert "power:force_restart" in result.ledger_row
    assert "host=h9" in result.ledger_row
    assert TOKEN not in result.model_dump_json()


def test_the_journal_intent_is_written_before_the_reset_is_sent() -> None:
    h = approved("h9", "power:force_restart")
    seen: list[int] = []

    def before_write(method: str, path: str) -> None:
        if method == "POST" and path.endswith("computersystem.reset/"):
            seen.append(len(h.journal.records))

    h.device.before_write = before_write
    h.handler.handle(power("h9", EnumIloPowerAction.FORCE_RESTART))
    assert seen == [1]  # exactly the intent record existed when the reset went out


def test_power_on_when_already_on_is_a_noop_and_sends_nothing() -> None:
    h = approved("h9", "power:on")
    result = h.handler.handle(power("h9", EnumIloPowerAction.ON))
    assert result.outcome is EnumIloOutcome.NOOP
    assert h.device.write_calls() == []
    assert h.journal.records == []


def test_force_off_then_on_changes_state_and_each_needs_its_own_approval() -> None:
    off_ref, on_ref = "2026-11-02T11:00:00Z", "2026-11-02T11:05:00Z"
    h = build(
        "gen9",
        approvals=(
            consent(scope_for("h9", "power:force_off"), ref=off_ref),
            consent(scope_for("h9", "power:on"), ref=on_ref),
        ),
    )
    assert (
        h.handler.handle(power("h9", EnumIloPowerAction.FORCE_OFF, ref=off_ref)).outcome
        is EnumIloOutcome.OK
    )
    assert h.device.resources[SYSTEM]["PowerState"] == "Off"
    assert (
        h.handler.handle(power("h9", EnumIloPowerAction.ON, ref=on_ref)).outcome
        is EnumIloOutcome.OK
    )
    assert h.device.resources[SYSTEM]["PowerState"] == "On"
    # An approval for power:on cannot be cited for force_off.
    refused = h.handler.handle(power("h9", EnumIloPowerAction.FORCE_OFF, ref=on_ref))
    assert refused.refusal is EnumIloRefusal.APPROVAL_INVALID


def test_restart_of_a_powered_off_server_is_a_precondition_failure() -> None:
    h = approved("h9", "power:force_restart")
    h.device.resources[SYSTEM]["PowerState"] = "Off"
    result = h.handler.handle(power("h9", EnumIloPowerAction.FORCE_RESTART))
    assert result.refusal is EnumIloRefusal.PRECONDITION_FAILED
    assert h.device.write_calls() == []


def test_a_reset_type_the_device_does_not_allow_is_refused() -> None:
    h = approved("h9", "power:force_restart")
    put(
        h.device.resources[SYSTEM],
        ("Actions", "#ComputerSystem.Reset", "ResetType@Redfish.AllowableValues"),
        ["On"],
    )
    result = h.handler.handle(power("h9", EnumIloPowerAction.FORCE_RESTART))
    assert result.refusal is EnumIloRefusal.UNSUPPORTED_VALUE


def test_a_state_that_never_confirms_is_reported_unverified_not_ok() -> None:
    ref_off = "2026-11-02T11:00:00Z"
    h = build(
        "gen9", approvals=(consent(scope_for("h9", "power:force_off"), ref=ref_off),)
    )
    h.device.no_effect_on_reset = True
    result = h.handler.handle(power("h9", EnumIloPowerAction.FORCE_OFF, ref=ref_off))
    assert result.outcome is EnumIloOutcome.UNVERIFIED
    assert h.journal.records[-1].status is EnumIloJournalStatus.UNVERIFIED


# -- retries ---------------------------------------------------------------------------------


def test_retry_not_repeated_after_a_completed_power_action() -> None:
    h = approved("h9", "power:force_restart")
    first = h.handler.handle(power("h9", EnumIloPowerAction.FORCE_RESTART))
    assert first.outcome is EnumIloOutcome.OK
    second = h.handler.handle(power("h9", EnumIloPowerAction.FORCE_RESTART))
    assert second.outcome is EnumIloOutcome.ALREADY_ATTEMPTED
    assert second.idempotency_key == first.idempotency_key
    resets = [
        c for c in h.device.write_calls() if c[1].endswith("computersystem.reset/")
    ]
    assert len(resets) == 1
    assert any(
        e.step == "journal" and "verified" in e.observed for e in second.evidence
    )
    assert any(e.step == "live" for e in second.evidence)


def test_retry_not_repeated_when_the_process_died_after_the_intent_was_recorded() -> (
    None
):
    h = approved("h9", "power:force_restart")
    # Simulate a crash: only the intent line reached the journal.
    first = h.handler.handle(power("h9", EnumIloPowerAction.FORCE_RESTART))
    h.journal.records[:] = h.journal.records[:1]
    h.device.calls.clear()
    second = h.handler.handle(power("h9", EnumIloPowerAction.FORCE_RESTART))
    assert second.outcome is EnumIloOutcome.ALREADY_ATTEMPTED
    assert h.device.write_calls() == []
    assert first.idempotency_key == second.idempotency_key


def test_retry_not_repeated_after_an_unknown_outcome() -> None:
    h = approved("h9", "power:force_restart")
    h.device.fail_write_after_send = True
    first = h.handler.handle(power("h9", EnumIloPowerAction.FORCE_RESTART))
    assert first.outcome is EnumIloOutcome.UNKNOWN
    assert h.journal.records[-1].status is EnumIloJournalStatus.UNKNOWN
    h.device.fail_write_after_send = False
    h.device.calls.clear()
    second = h.handler.handle(power("h9", EnumIloPowerAction.FORCE_RESTART))
    assert second.outcome is EnumIloOutcome.ALREADY_ATTEMPTED
    assert h.device.write_calls() == []


def test_a_rejected_write_may_be_retried_with_the_same_approval() -> None:
    h = approved("h9", "power:force_restart")
    h.device.deny_writes_with = 403
    first = h.handler.handle(power("h9", EnumIloPowerAction.FORCE_RESTART))
    assert first.outcome is EnumIloOutcome.FAILED
    assert first.refusal is EnumIloRefusal.INSUFFICIENT_PRIVILEGE
    assert "InsufficientPrivilege" in (first.detail or "")
    assert h.journal.records[-1].status is EnumIloJournalStatus.REJECTED
    h.device.deny_writes_with = None
    second = h.handler.handle(power("h9", EnumIloPowerAction.FORCE_RESTART))
    assert second.outcome is EnumIloOutcome.OK


def test_a_new_approval_is_a_new_key_and_may_run_again() -> None:
    ref2 = "2026-11-02T11:30:00Z"
    h = build(
        "gen9",
        approvals=(
            consent(scope_for("h9", "power:force_restart")),
            consent(scope_for("h9", "power:force_restart"), ref=ref2),
        ),
    )
    a = h.handler.handle(power("h9", EnumIloPowerAction.FORCE_RESTART))
    b = h.handler.handle(power("h9", EnumIloPowerAction.FORCE_RESTART, ref=ref2))
    assert a.idempotency_key != b.idempotency_key
    assert b.outcome is EnumIloOutcome.OK


# -- virtual media ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["h9", "h8"])
def test_media_insert_then_retry_then_eject(host: str) -> None:
    h = approved(host, "virtual_media:insert", IMAGE_URL)
    done = h.handler.handle(media(host, EnumIloMediaAction.INSERT, IMAGE_URL))
    assert done.outcome is EnumIloOutcome.OK
    assert h.device.resources[CD_MEMBER]["Inserted"] is True
    assert h.device.write_calls()[0][2] == {"Image": IMAGE_URL}
    again = h.handler.handle(media(host, EnumIloMediaAction.INSERT, IMAGE_URL))
    assert again.outcome is EnumIloOutcome.ALREADY_ATTEMPTED
    assert len(h.device.write_calls()) == 1


def test_media_insert_of_the_image_already_inserted_is_a_noop() -> None:
    h = approved("h9", "virtual_media:insert", IMAGE_URL)
    h.device.resources[CD_MEMBER].update({"Inserted": True, "Image": IMAGE_URL})
    result = h.handler.handle(media("h9", EnumIloMediaAction.INSERT, IMAGE_URL))
    assert result.outcome is EnumIloOutcome.NOOP
    assert h.device.write_calls() == []


def test_media_insert_over_a_different_image_needs_an_eject_first() -> None:
    h = approved("h9", "virtual_media:insert", IMAGE_URL)
    h.device.resources[CD_MEMBER].update(
        {"Inserted": True, "Image": "http://192.0.2.10/iso/old.iso"}
    )
    result = h.handler.handle(media("h9", EnumIloMediaAction.INSERT, IMAGE_URL))
    assert result.refusal is EnumIloRefusal.PRECONDITION_FAILED
    assert h.device.write_calls() == []


def test_media_eject_removes_the_image_and_a_second_eject_is_a_noop() -> None:
    ref1, ref2 = "2026-11-02T11:00:00Z", "2026-11-02T11:10:00Z"
    h = build(
        "gen9",
        approvals=(
            consent(scope_for("h9", "virtual_media:eject"), ref=ref1),
            consent(scope_for("h9", "virtual_media:eject"), ref=ref2),
        ),
    )
    h.device.resources[CD_MEMBER].update({"Inserted": True, "Image": IMAGE_URL})
    assert (
        h.handler.handle(media("h9", EnumIloMediaAction.EJECT, ref=ref1)).outcome
        is EnumIloOutcome.OK
    )
    assert h.device.resources[CD_MEMBER]["Inserted"] is False
    assert (
        h.handler.handle(media("h9", EnumIloMediaAction.EJECT, ref=ref2)).outcome
        is EnumIloOutcome.NOOP
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://192.0.2.99/iso/installer.iso",  # outside the overlay prefix
        "http://user:pw@192.0.2.10/iso/installer.iso",  # credentials in the URL
        "http://192.0.2.10/iso/installer.iso?token=abc",  # query string
        "http://192.0.2.10/iso/installer.img",  # not an ISO
        "ftp://192.0.2.10/iso/installer.iso",  # wrong scheme
    ],
)
def test_media_urls_outside_the_overlay_policy_are_refused(url: str) -> None:
    h = approved("h9", "virtual_media:insert", url)
    result = h.handler.handle(media("h9", EnumIloMediaAction.INSERT, url))
    assert result.refusal is EnumIloRefusal.MEDIA_URL_NOT_ALLOWED
    assert h.device.write_calls() == []
    assert "user:pw" not in (result.ledger_row or "") + (result.detail or "")


def test_media_without_a_cd_member_is_endpoint_missing() -> None:
    h = approved("h9", "virtual_media:insert", IMAGE_URL)
    h.device.resources[CD_MEMBER]["MediaTypes"] = ["Floppy"]
    result = h.handler.handle(media("h9", EnumIloMediaAction.INSERT, IMAGE_URL))
    assert result.refusal is EnumIloRefusal.ENDPOINT_MISSING


# -- boot once -------------------------------------------------------------------------------


def test_gen9_boot_once_sets_the_target_before_the_enabled_value_then_verifies() -> (
    None
):
    h = approved("h9", "boot_once:cd")
    result = h.handler.handle(boot("h9", EnumIloBootTarget.CD))
    assert result.outcome is EnumIloOutcome.OK
    assert h.device.write_calls() == [
        ("PATCH", SYSTEM, {"Boot": {"BootSourceOverrideTarget": "Cd"}}),
        ("PATCH", SYSTEM, {"Boot": {"BootSourceOverrideEnabled": "Once"}}),
    ]
    again = h.handler.handle(boot("h9", EnumIloBootTarget.CD))
    assert again.outcome is EnumIloOutcome.ALREADY_ATTEMPTED
    assert len(h.device.write_calls()) == 2


def test_gen9_boot_once_already_set_is_a_noop() -> None:
    h = approved("h9", "boot_once:pxe")
    put(h.device.resources[SYSTEM], ("Boot", "BootSourceOverrideTarget"), "Pxe")
    put(h.device.resources[SYSTEM], ("Boot", "BootSourceOverrideEnabled"), "Once")
    assert (
        h.handler.handle(boot("h9", EnumIloBootTarget.PXE)).outcome
        is EnumIloOutcome.NOOP
    )
    assert h.device.write_calls() == []


def test_boot_target_not_in_the_advertised_values_is_refused() -> None:
    h = approved("h9", "boot_once:usb")
    put(
        h.device.resources[SYSTEM],
        ("Boot", "BootSourceOverrideTarget@Redfish.AllowableValues"),
        ["None", "Pxe"],
    )
    result = h.handler.handle(boot("h9", EnumIloBootTarget.USB))
    assert result.refusal is EnumIloRefusal.UNSUPPORTED_VALUE


def test_gen8_without_a_boot_override_refuses_pxe_instead_of_guessing() -> None:
    h = approved("h8", "boot_once:pxe")
    result = h.handler.handle(boot("h8", EnumIloBootTarget.PXE))
    assert result.refusal is EnumIloRefusal.ENDPOINT_MISSING
    assert h.device.write_calls() == []


def test_gen8_boot_once_from_cd_uses_the_virtual_media_boot_on_next_reset_flag() -> (
    None
):
    h = approved("h8", "boot_once:cd")
    h.device.resources[CD_MEMBER].update({"Inserted": True, "Image": IMAGE_URL})
    result = h.handler.handle(boot("h8", EnumIloBootTarget.CD))
    assert result.outcome is EnumIloOutcome.OK
    assert h.device.write_calls() == [
        ("PATCH", CD_MEMBER, {"Oem": {"Hp": {"BootOnNextServerReset": True}}})
    ]


def test_gen8_boot_once_from_cd_needs_the_media_inserted_first() -> None:
    h = approved("h8", "boot_once:cd")
    result = h.handler.handle(boot("h8", EnumIloBootTarget.CD))
    assert result.refusal is EnumIloRefusal.PRECONDITION_FAILED


# -- records ---------------------------------------------------------------------------------


def test_every_write_result_carries_one_pipe_free_redacted_ledger_row() -> None:
    h = approved("h9", "power:force_restart")
    h.device.resources[SYSTEM]["Model"] = f"ProLiant DL360 Gen9 {LICENSE_KEY}"
    result = h.handler.handle(power("h9", EnumIloPowerAction.FORCE_RESTART))
    row = result.ledger_row
    assert row is not None
    assert "|" not in row
    assert "\n" not in row
    assert LICENSE_KEY not in row
    assert LICENSE_KEY not in result.model_dump_json()
    for needle in (f"approval={APPROVAL_REF}", "requested_by=lane-a", "outcome="):
        assert needle in row
