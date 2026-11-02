"""Shared wiring for the iLO Redfish effect tests (OMN-21835): a handler over mocked ports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

from omnimarket.nodes.node_ilo_redfish_effect.handlers.handler_ilo_redfish import (
    HandlerIloRedfish,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.protocols import (
    ProtocolRedfishTransport,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_overlay import (
    ModelIloHost,
    ModelIloOverlay,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_redfish_result import (
    ModelIloApproval,
)
from tests.nodes.node_ilo_redfish_effect.ilo_fakes import (
    GEN8_SERIAL,
    GEN9_SERIAL,
    IMAGE_URL,
    NOW,
    FakeIloTransport,
    MapApprovals,
    MapCredentials,
    MemoryJournal,
    clock,
    ilo_resources,
    no_sleep,
)

TEMPLATE = "ILO_{HOST}_{ROLE}_{FIELD}"
APPROVAL_REF = "2026-11-02T11:00:00Z"
OPERATOR_LANE = "lane-a"


def host_row(
    generation: Literal["gen8", "gen9"],
    *,
    live: bool = True,
    serial: str | None = "default",
) -> ModelIloHost:
    gen8 = generation == "gen8"
    expected = GEN8_SERIAL if gen8 else GEN9_SERIAL
    return ModelIloHost.model_validate(
        {
            "name": "h8" if gen8 else "h9",
            "generation": generation,
            "address": "192.0.2.8" if gen8 else "192.0.2.9",
            "expected_product": "DL380p Gen8" if gen8 else "DL360 Gen9",
            "expected_serial": expected if serial == "default" else serial,
            "tls": {"ca_file": "certs/test.pem", "sha256": "a" * 64},
            "live": live,
            "media_url_prefixes": ["http://192.0.2.10/iso/"],
        }
    )


def consent(
    scope: str,
    *,
    ref: str = APPROVAL_REF,
    lane: str | None = "orchestrator-x",
    approved_by: str = "operator",
    age: timedelta = timedelta(hours=1),
) -> ModelIloApproval:
    return ModelIloApproval(
        ref=ref,
        at=NOW - age,
        lane=lane,
        approved_by=approved_by,
        approved_scope=scope,
        out_of_scope="everything else",
    )


def scope_for(host: str, action: str, image: str | None = None) -> str:
    base = f"ilo-redfish host={host} action={action}"
    return f"{base} image={image}" if image else base


@dataclass
class Harness:
    handler: HandlerIloRedfish
    device: FakeIloTransport
    journal: MemoryJournal
    credentials: MapCredentials
    factory_calls: list[str] = field(default_factory=list)


def build(
    generation: Literal["gen8", "gen9"] = "gen9",
    *,
    live: bool = True,
    serial: str | None = "default",
    approvals: tuple[ModelIloApproval, ...] = (),
    credentials: dict[str, str] | None = None,
    journal: MemoryJournal | None = None,
    device: FakeIloTransport | None = None,
    transport_factory: Callable[[ModelIloHost], ProtocolRedfishTransport] | None = None,
) -> Harness:
    row = host_row(generation, live=live, serial=serial)
    overlay = ModelIloOverlay(hosts=(row,), journal_dir="/unused")
    fake = device or FakeIloTransport(ilo_resources(generation))
    store = MapCredentials(
        credentials
        if credentials is not None
        else {
            f"ILO_{row.name.upper()}_READ_USERNAME": "reader",
            f"ILO_{row.name.upper()}_READ_PASSWORD": "reader-pass-value",
            f"ILO_{row.name.upper()}_WRITE_USERNAME": "operator",
            f"ILO_{row.name.upper()}_WRITE_PASSWORD": "operator-pass-value",
        }
    )
    journal = journal or MemoryJournal()
    calls: list[str] = []

    def factory(host: ModelIloHost) -> ProtocolRedfishTransport:
        calls.append(host.name)
        if transport_factory is not None:
            return transport_factory(host)
        return fake

    handler = HandlerIloRedfish(
        overlay=overlay,
        transport_factory=factory,
        credentials=store,
        approvals=MapApprovals(*approvals),
        journal=journal,
        clock=clock,
        sleeper=no_sleep,
        credential_key_template=TEMPLATE,
        verify_timeout_s=2.0,
        poll_interval_s=1.0,
    )
    return Harness(handler, fake, journal, store, calls)


__all__ = [
    "APPROVAL_REF",
    "IMAGE_URL",
    "OPERATOR_LANE",
    "Harness",
    "build",
    "consent",
    "host_row",
    "scope_for",
]
