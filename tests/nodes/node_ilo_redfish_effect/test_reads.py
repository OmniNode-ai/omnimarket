"""Read verbs against mocked iLO 4 Gen8 and Gen9 shapes (OMN-21835). No network."""

from __future__ import annotations

import json
from typing import Literal

import pytest

from omnimarket.nodes.node_ilo_redfish_effect.handlers.protocols import (
    ProtocolRedfishTransport,
    RedfishTransportError,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.enum_ilo_redfish import (
    EnumIloLogKind,
    EnumIloOutcome,
    EnumIloRefusal,
    EnumIloVerb,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_redfish_request import (
    ModelIloRedfishRequest,
)
from tests.nodes.node_ilo_redfish_effect.conftest import OPERATOR_LANE, build
from tests.nodes.node_ilo_redfish_effect.ilo_fakes import (
    GEN8_SERIAL,
    GEN9_SERIAL,
    LICENSE_KEY,
    TOKEN,
    dig,
)

pytestmark = pytest.mark.unit


def _req(host: str, verb: EnumIloVerb, **extra: object) -> ModelIloRedfishRequest:
    return ModelIloRedfishRequest.model_validate(
        {"verb": verb, "host": host, "requested_by": OPERATOR_LANE, **extra}
    )


GENERATIONS: list[tuple[Literal["gen8", "gen9"], str, str]] = [
    ("gen9", "h9", GEN9_SERIAL),
    ("gen8", "h8", GEN8_SERIAL),
]


@pytest.mark.parametrize(("generation", "host", "serial"), GENERATIONS)
def test_discover_follows_links_and_reports_firmware(
    generation: Literal["gen8", "gen9"], host: str, serial: str
) -> None:
    h = build(generation)
    result = h.handler.handle(_req(host, EnumIloVerb.DISCOVER))
    assert result.outcome is EnumIloOutcome.OK
    assert result.data["systems"] == ["/redfish/v1/systems/1/"]
    assert result.data["ilo_firmware"] == "iLO 4 v2.82"
    assert result.data["generation_declared"] == generation
    assert result.data["license"] == {"LicenseType": "Perpetual"}


@pytest.mark.parametrize(("generation", "host", "serial"), GENERATIONS)
def test_identity_matches_overlay(
    generation: Literal["gen8", "gen9"], host: str, serial: str
) -> None:
    h = build(generation)
    result = h.handler.handle(_req(host, EnumIloVerb.IDENTITY))
    assert result.data["serial"] == serial
    assert result.data["serial_matches_overlay"] is True
    assert result.data["product_matches_overlay"] is True
    assert result.data["generation_consistent"] is True


def test_identity_read_reports_a_mismatch_without_refusing() -> None:
    h = build("gen9", serial="SOMEOTHERSERIAL")
    result = h.handler.handle(_req("h9", EnumIloVerb.IDENTITY))
    assert result.outcome is EnumIloOutcome.OK
    assert result.data["serial_matches_overlay"] is False


def test_identity_read_works_while_the_overlay_has_no_serial_yet() -> None:
    h = build("gen9", serial=None)
    result = h.handler.handle(_req("h9", EnumIloVerb.IDENTITY))
    assert result.data["serial"] == GEN9_SERIAL
    assert result.data["serial_matches_overlay"] is None
    assert result.data["overlay_serial_set"] is False


def test_a_gen8_overlay_over_a_gen9_system_is_flagged_inconsistent() -> None:
    h = build("gen8")
    h.device.resources["/redfish/v1/systems/1/"]["Model"] = "ProLiant DL380p Gen9"
    result = h.handler.handle(_req("h8", EnumIloVerb.IDENTITY))
    assert result.data["generation_consistent"] is False


@pytest.mark.parametrize(("generation", "host", "serial"), GENERATIONS)
def test_inventory_and_health_and_power_status(
    generation: Literal["gen8", "gen9"], host: str, serial: str
) -> None:
    h = build(generation)
    inventory = h.handler.handle(_req(host, EnumIloVerb.INVENTORY))
    model = dig(inventory.data, "system", "Model")
    assert isinstance(model, str)
    assert model.endswith(generation.capitalize())
    assert dig(inventory.data, "Processors", 0, "TotalCores") == 8
    assert dig(inventory.data, "unavailable") == ["EthernetInterfaces"]
    health = h.handler.handle(_req(host, EnumIloVerb.HEALTH))
    assert dig(health.data, "system", "Status", "Health") == "OK"
    assert dig(health.data, "chassis", 0, "temperatures", 0, "Name") == "Inlet"
    power = h.handler.handle(_req(host, EnumIloVerb.POWER_STATUS))
    assert power.data["PowerState"] == "On"


def test_logs_are_newest_first_bounded_and_license_keys_are_masked() -> None:
    h = build("gen9")
    result = h.handler.handle(
        _req("h9", EnumIloVerb.LOGS, log_kind=EnumIloLogKind.IML, log_limit=2)
    )
    assert dig(result.data, "entries", 0, "Created") == "2026-03-01T00:00:00Z"
    assert dig(result.data, "entries", 1, "Created") == "2026-02-01T00:00:00Z"
    entries = result.data["entries"]
    assert isinstance(entries, list)
    assert len(entries) == 2
    assert LICENSE_KEY not in result.model_dump_json()
    message = dig(result.data, "entries", 0, "Message")
    assert isinstance(message, str)
    assert "[REDACTED]" in message


def test_event_log_is_read_from_the_manager() -> None:
    h = build("gen8")
    result = h.handler.handle(_req("h8", EnumIloVerb.LOGS, log_kind=EnumIloLogKind.IEL))
    assert dig(result.data, "entries", 0, "Message") == "iLO reset"


def test_a_read_issues_no_write_and_never_leaks_the_token_or_license_key() -> None:
    h = build("gen9")
    for verb in (EnumIloVerb.DISCOVER, EnumIloVerb.INVENTORY, EnumIloVerb.HEALTH):
        result = h.handler.handle(_req("h9", verb))
        assert TOKEN not in result.model_dump_json()
        assert LICENSE_KEY not in result.model_dump_json()
    assert h.device.write_calls() == []
    # The only non-GET calls are the session login and the session logout.
    non_get = [(m, p) for m, p, _, _ in h.device.calls if m != "GET"]
    assert {m for m, _ in non_get} == {"POST", "DELETE"}
    assert all(p == "/redfish/v1/sessions/" or "sessions" in p for _, p in non_get)
    assert h.device.session_logged_in is False


def test_the_token_is_sent_only_after_login() -> None:
    h = build("gen9")
    h.handler.handle(_req("h9", EnumIloVerb.POWER_STATUS))
    first_authenticated = next(
        i for i, (_, _, _, tok) in enumerate(h.device.calls) if tok
    )
    login = next(
        i
        for i, (m, p, _, _) in enumerate(h.device.calls)
        if m == "POST" and "sessions" in p
    )
    assert login < first_authenticated


def test_a_host_that_is_not_live_is_refused_without_opening_a_connection() -> None:
    h = build("gen9", live=False)
    result = h.handler.handle(_req("h9", EnumIloVerb.POWER_STATUS))
    assert result.outcome is EnumIloOutcome.REFUSED
    assert result.refusal is EnumIloRefusal.HOST_NOT_LIVE
    assert h.factory_calls == []
    assert h.credentials.asked == []


def test_an_unknown_host_is_refused() -> None:
    h = build("gen9")
    result = h.handler.handle(_req("nope", EnumIloVerb.POWER_STATUS))
    assert result.refusal is EnumIloRefusal.HOST_NOT_CONFIGURED


def test_a_missing_read_credential_is_refused_before_any_connection() -> None:
    h = build("gen9", credentials={})
    result = h.handler.handle(_req("h9", EnumIloVerb.POWER_STATUS))
    assert result.refusal is EnumIloRefusal.CREDENTIAL_MISSING
    assert h.factory_calls == []
    assert h.credentials.asked == ["ILO_H9_READ_USERNAME", "ILO_H9_READ_PASSWORD"]


def test_a_rejected_login_is_reported_as_auth_failed() -> None:
    h = build(
        "gen9",
        credentials={"ILO_H9_READ_USERNAME": "wrong", "ILO_H9_READ_PASSWORD": "x"},
    )
    result = h.handler.handle(_req("h9", EnumIloVerb.POWER_STATUS))
    assert result.refusal is EnumIloRefusal.AUTH_FAILED
    assert "wrong" not in result.model_dump_json()


def test_a_transport_tls_failure_is_a_refusal_and_never_a_retry() -> None:

    def failing(_: object) -> ProtocolRedfishTransport:
        raise RedfishTransportError("tls", request_sent=False)

    h = build("gen9", transport_factory=failing)
    result = h.handler.handle(_req("h9", EnumIloVerb.POWER_STATUS))
    assert result.refusal is EnumIloRefusal.TLS_FAILED


def test_two_systems_under_one_ilo_are_ambiguous() -> None:
    h = build("gen9")
    h.device.resources["/redfish/v1/systems/"]["Members"] = [
        {"@odata.id": "/redfish/v1/systems/1/"},
        {"@odata.id": "/redfish/v1/systems/2/"},
    ]
    result = h.handler.handle(_req("h9", EnumIloVerb.POWER_STATUS))
    assert result.refusal is EnumIloRefusal.AMBIGUOUS_ENDPOINT


def test_a_link_leaving_the_redfish_tree_is_not_followed() -> None:
    h = build("gen9")
    h.device.resources["/redfish/v1/"]["Systems"] = {
        "@odata.id": "https://203.0.113.5/elsewhere/"
    }
    result = h.handler.handle(_req("h9", EnumIloVerb.POWER_STATUS))
    assert result.refusal is EnumIloRefusal.ENDPOINT_MISSING
    assert all("elsewhere" not in p for _, p, _, _ in h.device.calls)


def test_result_json_round_trips_without_secrets() -> None:
    h = build("gen9")
    result = h.handler.handle(_req("h9", EnumIloVerb.DISCOVER))
    text = result.model_dump_json()
    assert "operator-pass-value" not in text
    assert "reader-pass-value" not in text
    assert json.loads(text)["outcome"] == "ok"
