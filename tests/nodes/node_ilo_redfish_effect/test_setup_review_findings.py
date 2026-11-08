"""Reviewer findings on the iLO setup verb (OMN-22870): trust before credentials, a refused rerun,
fingerprint evidence, the configured port, the overlay write.

Each test fails on the code the review was written against.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import JsonValue

from omnimarket.nodes.node_ilo_redfish_effect.handlers import (
    ilo_setup_runner as runner,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers import (
    setup_certificate as cert,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.protocols import (
    ProtocolRedfishTransport,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_overlay import (
    ModelIloHost,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_setup import (
    EnumIloSetupRefusal as R,
)
from tests.nodes.node_ilo_redfish_effect.test_setup import (
    Rig,
    SetupDevice,
    make_certificate,
)

pytestmark = pytest.mark.unit


def _signed_in(device: SetupDevice) -> bool:
    return any(
        m == "POST" and p == "/redfish/v1/sessions/" for m, p, _, _ in device.calls
    )


def _edit_overlay(rig: Rig, change: Any) -> None:
    path = rig.overlay_dir / "overlay.yaml"
    document = yaml.safe_load(path.read_text())
    change(document["hosts"][0])
    path.write_text(yaml.safe_dump(document))


# -- thread 5: trust is established before the administrator password is sent ------------------


def test_an_unfamiliar_self_signed_certificate_is_confirmed_before_any_sign_in(
    tmp_path: Path,
) -> None:
    device = SetupDevice("gen9")
    rig = Rig(tmp_path, {"h9": device}, confirm=False)
    result = rig.run("h9")
    assert result.hosts[0].refusal is R.FINGERPRINT_UNCONFIRMED
    assert len(rig.confirmations) == 1
    assert not _signed_in(device)
    assert not (rig.overlay_dir / "overlay.yaml").exists()


def test_a_confirmed_self_signed_certificate_is_trusted_and_a_pinned_rerun_asks_nothing(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, {"h9": SetupDevice("gen9")})
    assert rig.run("h9").hosts[0].status == "ready"
    assert len(rig.confirmations) == 1
    assert rig.run("h9").hosts[0].status == "ready"
    assert len(rig.confirmations) == 1


def test_a_certificate_that_differs_from_the_recorded_pin_is_refused_before_any_sign_in(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, {"h9": SetupDevice("gen9")})
    assert rig.run("h9").hosts[0].status == "ready"
    impostor = SetupDevice("gen9")
    rig.devices["h9"] = impostor
    rig.der = make_certificate("impostor")
    result = rig.run("h9")
    assert result.hosts[0].refusal is R.FINGERPRINT_MISMATCH
    assert not _signed_in(impostor)
    assert len(rig.confirmations) == 1


# -- thread 6: a refused candidate leaves the working host as it was ----------------------------


def test_a_refused_rerun_keeps_the_working_row_and_certificate(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"h9": SetupDevice("gen9")})
    assert rig.run("h9").hosts[0].status == "ready"
    before = rig.overlay()
    pem = (rig.overlay_dir / "h9-ilo.pem").read_bytes()
    rig.devices["h9"].resources["/redfish/v1/systems/1/"]["SerialNumber"] = (
        "OTHERSERIAL"
    )
    result = rig.run("h9", addresses={"h9": "192.0.2.99"})
    assert result.hosts[0].refusal is R.IDENTITY_MISMATCH
    assert rig.overlay() == before
    assert before["hosts"][0]["live"] is True
    assert (rig.overlay_dir / "h9-ilo.pem").read_bytes() == pem


def test_a_first_read_that_fails_publishes_no_row_and_no_certificate(
    tmp_path: Path,
) -> None:
    device = SetupDevice("gen9")
    del device.resources["/redfish/v1/systems/1/"]["SerialNumber"]
    rig = Rig(tmp_path, {"h9": device})
    result = rig.run("h9")
    assert result.hosts[0].status == "refused"
    assert not (rig.overlay_dir / "overlay.yaml").exists()
    assert not (rig.overlay_dir / "h9-ilo.pem").exists()


# -- thread 7: fingerprint evidence ---------------------------------------------------------------

SEEN = cert.PresentedCertificate(
    sha256="cd" * 32, serial="1", subject="CN=x", not_after="2030-01-01T00:00:00Z"
)


def _told(info: dict[str, JsonValue]) -> cert.ReportedCertificate:
    return cert.reported({"X509CertificateInformation": {"SerialNumber": "1", **info}})


def test_a_generic_sha1_fingerprint_does_not_hide_a_reported_sha256_mismatch() -> None:
    told = _told({"Fingerprint": "ab" * 20, "SHA256Fingerprint": "ef" * 32})
    assert not cert.matches(SEEN, told)


def test_an_earlier_sha1_only_object_does_not_hide_a_later_sha256() -> None:
    body: dict[str, JsonValue] = {
        "Older": {"SHA1Fingerprint": "ab" * 20},
        "X509CertificateInformation": {
            "SerialNumber": "1",
            "SHA256Fingerprint": "ef" * 32,
        },
    }
    assert not cert.matches(SEEN, cert.reported(body))


def test_a_malformed_explicit_sha256_is_not_accepted_as_absent() -> None:
    assert not cert.matches(SEEN, _told({"SHA256Fingerprint": "not-a-fingerprint"}))
    assert not cert.matches(SEEN, _told({"SHA256Fingerprint": "ab" * 31}))


def test_a_matching_sha256_after_a_sha1_field_is_still_accepted() -> None:
    told = _told({"Fingerprint": "ab" * 20, "SHA256Fingerprint": ("cd" * 32).upper()})
    assert cert.matches(SEEN, told)


def test_no_sha256_evidence_at_all_is_still_a_serial_only_match() -> None:
    assert cert.matches(SEEN, _told({"Fingerprint": "ab" * 20}))
    assert cert.matches(SEEN, _told({}))


def test_the_same_bad_sha256_is_refused_through_the_setup(tmp_path: Path) -> None:
    device = SetupDevice("gen9", reported_sha256="zz")
    rig = Rig(tmp_path, {"h9": device})
    result = rig.run("h9")
    assert result.hosts[0].refusal is R.FINGERPRINT_MISMATCH
    assert device.account_posts == []


# -- thread 9: one endpoint for the certificate fetch, the provisioning and the first read -------


def test_a_host_configured_on_another_port_is_set_up_on_that_port_throughout(
    tmp_path: Path,
) -> None:
    ports: list[int] = []
    devices = {"h9": SetupDevice("gen9")}

    def factory(host: ModelIloHost, directory: Path) -> ProtocolRedfishTransport:
        del directory
        ports.append(host.port)
        return devices[host.name]

    rig = Rig(tmp_path, devices, transport_factory=factory)
    assert rig.run("h9").hosts[0].status == "ready"
    _edit_overlay(rig, lambda row: row.__setitem__("port", 8443))
    rig.fetches.clear()
    ports.clear()
    assert rig.run("h9").hosts[0].status == "ready"
    assert [port for _, port in rig.fetches] == [8443]
    assert ports
    assert set(ports) == {8443}
    assert rig.overlay()["hosts"][0]["port"] == 8443


def test_a_new_host_is_set_up_on_the_default_port(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"h9": SetupDevice("gen9")})
    assert rig.run("h9").hosts[0].status == "ready"
    assert [port for _, port in rig.fetches] == [443]


# -- threads 11, 12, 13: the overlay and the files it points at --------------------------------


def _row(name: str) -> dict[str, JsonValue]:
    return runner._host_row(name, "gen9", "192.0.2.1", "Gen9", "ab" * 32, "x.pem", True)


def test_two_writers_staging_the_same_file_each_publish_only_their_own_complete_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, {"h9": SetupDevice("gen9")})
    target = tmp_path / "published" / "file.pem"
    real_replace = os.replace
    raced: list[bool] = []

    def replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if not raced:
            # a second writer runs to completion between this writer's close and its rename
            raced.append(True)
            rig.handler._write_private(target, "B" * 100)
        real_replace(src, dst)

    monkeypatch.setattr(runner.os, "replace", replace)
    rig.handler._write_private(target, "A" * 100)
    assert target.read_text() in ("A" * 100, "B" * 100)
    assert sorted(p.name for p in target.parent.iterdir()) == ["file.pem"]


def test_concurrent_host_updates_do_not_erase_each_others_overlay_rows(
    tmp_path: Path,
) -> None:
    first = Rig(tmp_path, {"h1": SetupDevice("gen9")}).handler
    second = Rig(tmp_path, {"h2": SetupDevice("gen9")}).handler
    other = threading.Thread(target=lambda: second._write_overlay(_row("h2")))
    real_document = first._overlay_document

    def document_then_the_other_host_writes() -> dict[str, JsonValue]:
        read = real_document()
        other.start()
        other.join(timeout=0.5)
        return read

    first._overlay_document = document_then_the_other_host_writes  # type: ignore[method-assign]
    first._write_overlay(_row("h1"))
    other.join()
    document = yaml.safe_load(
        (tmp_path / "overlays/node_ilo_redfish_effect/overlay.yaml").read_text()
    )
    assert sorted(r["name"] for r in document["hosts"]) == ["h1", "h2"]


def test_a_journal_directory_that_cannot_be_made_refuses_that_host_and_the_batch_goes_on(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, {"h8": SetupDevice("gen8"), "h9": SetupDevice("gen9")})
    rig.journal_dir.write_text("a file where the journal directory should be")
    result = rig.run("h8", "h9")
    assert [h.host for h in result.hosts] == ["h8", "h9"]
    assert [h.refusal for h in result.hosts] == [R.OVERLAY_WRITE_FAILED] * 2
