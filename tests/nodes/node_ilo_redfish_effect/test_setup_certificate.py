"""Weak-certificate replacement in the iLO setup verb (OMN-22870).

An iLO 4 on firmware 2.82 presents an RSA 1024-bit certificate that a modern client refuses. The setup
shows the fingerprint, asks the operator to confirm it against the iLO's own certificate page, has the
iLO generate a 2048-bit signing request, signs it with a lab CA that lives only in the secret store,
imports it, waits for the reset, connects again verifying the lab CA, and goes on.

Everything runs over a scripted Redfish device and a dict-backed store. The last tests use a real local
TLS server with an RSA 1024-bit certificate to prove the legacy transport's pin check and the chain
verification of the normal transport.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import socket
import ssl
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pydantic import JsonValue, SecretStr

from omnimarket.nodes.node_ilo_redfish_effect.handlers import (
    setup_certificate as cert,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers import setup_lab_ca as lab_ca
from omnimarket.nodes.node_ilo_redfish_effect.handlers.ilo_setup_runner import (
    RESET_POLLS,
    _LegacyScope,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.protocols import (
    ModelRedfishResponse,
    ProtocolRedfishTransport,
    RedfishTransportError,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.setup_adapters import (
    fetch_certificate_der,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.transport_pinned_https import (
    PinnedHttpsTransport,
    build_legacy_pinned_transport,
    build_pinned_transport,
    pinned_context,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_overlay import (
    ModelIloHost,
    ModelIloOverlay,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_setup import (
    EnumIloSetupRefusal as R,
)
from tests.nodes.node_ilo_redfish_effect.test_setup import (
    ADMIN_PASSWORD,
    NOW,
    Rig,
    SetupDevice,
)

pytestmark = pytest.mark.unit

Method = Literal["GET", "POST", "PATCH", "DELETE"]
HTTPS = "/redfish/v1/managers/1/securityservice/httpscert/"
GENERATE = f"{HTTPS}actions/hpehttpscert.generatecsr/"
IMPORT = f"{HTTPS}actions/hpehttpscert.importcertificate/"


# The iLO 4 firmware 2.82 certificate size the weak-certificate path exists for (twice the 512-bit halves).
LEGACY_ILO_KEY_BITS = 2 * 512


def _self_signed(bits: int, common_name: str, serial: int) -> tuple[bytes, bytes]:
    """(DER, key PEM) of a self-signed RSA certificate."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(NOW - dt.timedelta(days=1))
        .not_valid_after(NOW + dt.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.DER), key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _weak_der(serial: int = 0x1A2B3C4D) -> bytes:
    return _self_signed(LEGACY_ILO_KEY_BITS, "ILOUSE443DE5W", serial)[0]


class WeakDevice(SetupDevice):
    """An iLO 4 with a weak certificate: it lists the certificate actions, generates a CSR on request,
    imports a signed certificate, resets, and from then on presents the imported certificate."""

    def __init__(
        self,
        weak_der: bytes,
        *,
        style: Literal["redfish", "legacy", "none"] = "redfish",
        csr_bits: int = 2048,
        csr_polls: int = 2,
        reset_polls: int = 2,
        import_status: int = 200,
        drop_on_import: bool = False,
    ) -> None:
        weak = x509.load_der_x509_certificate(weak_der)
        super().__init__("gen9", reported_serial=format(weak.serial_number, "x"))
        self.weak_der = weak_der
        self.style = style
        self.csr_bits = csr_bits
        self.csr_polls = csr_polls
        self.reset_polls = reset_polls
        self.import_status = import_status
        self.drop_on_import = drop_on_import
        self.state: Literal["weak", "resetting", "new"] = "weak"
        self.reset_left = 0
        self.imported: x509.Certificate | None = None
        self.csr_key: rsa.RSAPrivateKey | None = None
        self.csr_pem = ""
        self.csr_gets = 0
        self.csr_params: dict[str, JsonValue] = {}
        self.legacy_pins: list[str] = []
        self.reported_override: str | None = None
        resource = self.resources[HTTPS]
        if style == "redfish":
            resource["Actions"] = {
                "#HpeHttpsCert.GenerateCSR": {"target": GENERATE},
                "#HpeHttpsCert.ImportCertificate": {"target": IMPORT},
            }
        elif style == "legacy":
            resource["AvailableActions"] = [
                {"Action": "GenerateCSR"},
                {"Action": "ImportCertificate"},
            ]

    # the resource the iLO reports about its own certificate follows the certificate it holds
    def request(
        self,
        method: Method,
        path: str,
        *,
        body: JsonValue = None,
        token: SecretStr | None = None,
    ) -> ModelRedfishResponse:
        if method == "GET" and path == HTTPS:
            current = self.imported or x509.load_der_x509_certificate(self.weak_der)
            info = self.resources[HTTPS]["X509CertificateInformation"]
            assert isinstance(info, dict)
            info["SerialNumber"] = self.reported_override or format(
                current.serial_number, "x"
            )
            if self.csr_key is not None:
                self.csr_gets += 1
                self.resources[HTTPS]["CertificateSigningRequest"] = (
                    self.csr_pem if self.csr_gets > self.csr_polls else ""
                )
        return super().request(method, path, body=body, token=token)

    def _write(self, method: str, path: str, body: JsonValue) -> ModelRedfishResponse:
        assert isinstance(body, dict)
        if method == "POST" and self.style != "none":
            named = body.get("Action")
            if path == GENERATE or (path == HTTPS and named == "GenerateCSR"):
                return self._generate(body)
            if path == IMPORT or (path == HTTPS and named == "ImportCertificate"):
                return self._import(body)
        return super()._write(method, path, body)

    def _generate(self, body: dict[str, JsonValue]) -> ModelRedfishResponse:
        self.csr_params = {k: v for k, v in body.items() if k != "Action"}
        self.csr_key = rsa.generate_private_key(
            public_exponent=65537, key_size=self.csr_bits
        )
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(
                x509.Name(
                    [x509.NameAttribute(NameOID.COMMON_NAME, str(body["CommonName"]))]
                )
            )
            .sign(self.csr_key, hashes.SHA256())
        )
        self.csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode("ascii")
        self.csr_gets = 0
        return ModelRedfishResponse(status=200, body={})

    def _import(self, body: dict[str, JsonValue]) -> ModelRedfishResponse:
        if self.import_status != 200:
            return ModelRedfishResponse(
                status=self.import_status,
                body={
                    "error": {
                        "@Message.ExtendedInfo": [
                            {"MessageId": "iLO.0.10.BadCertificate"}
                        ]
                    }
                },
            )
        leaf = x509.load_pem_x509_certificate(str(body["Certificate"]).encode("ascii"))
        assert self.csr_key is not None
        leaf_key = leaf.public_key()
        assert isinstance(leaf_key, rsa.RSAPublicKey)
        assert leaf_key.public_numbers() == self.csr_key.public_key().public_numbers()
        self.imported = leaf
        self.state = "resetting"
        self.reset_left = self.reset_polls
        if self.drop_on_import:
            raise RedfishTransportError("timeout", request_sent=True)
        return ModelRedfishResponse(status=200, body={})

    # -- what each kind of connection to this device can do ------------------------------------
    def view(self, kind: Literal["legacy", "verified"]) -> ProtocolRedfishTransport:
        return _View(self, kind)


class _View:
    def __init__(self, device: WeakDevice, kind: Literal["legacy", "verified"]) -> None:
        self._device = device
        self._kind = kind

    def request(
        self,
        method: Method,
        path: str,
        *,
        body: JsonValue = None,
        token: SecretStr | None = None,
    ) -> ModelRedfishResponse:
        device = self._device
        if self._kind == "legacy":
            if (
                device.state != "weak"
            ):  # the iLO now presents another certificate: the pin refuses
                raise RedfishTransportError("tls", request_sent=False)
        elif (
            device.state == "weak"
        ):  # the old certificate does not verify against the lab CA
            raise RedfishTransportError("tls", request_sent=False)
        elif device.state == "resetting":
            if device.reset_left > 0:
                device.reset_left -= 1
                raise RedfishTransportError("connect", request_sent=False)
            device.state = "new"
        return device.request(method, path, body=body, token=token)


class WeakRig(Rig):
    """A rig whose factories model the two connection kinds, verifying what the real ones verify."""

    def __init__(self, tmp_path: Path, device: WeakDevice, **kwargs: Any) -> None:
        self.device = device
        self.verified_dirs: list[Path] = []
        self.sleeps: list[float] = []

        def legacy_factory(
            host: ModelIloHost, directory: Path
        ) -> ProtocolRedfishTransport:
            device.legacy_pins.append(host.tls.sha256)
            if host.tls.sha256 != hashlib.sha256(device.weak_der).hexdigest():
                raise RedfishTransportError("tls", request_sent=False)
            return device.view("legacy")

        def factory(host: ModelIloHost, directory: Path) -> ProtocolRedfishTransport:
            if host.tls.issuer_file is None:
                return device.view("legacy")
            leaf = x509.load_pem_x509_certificate(
                Path(directory, host.tls.ca_file).read_bytes()
            )
            ca = x509.load_pem_x509_certificate(
                Path(directory, host.tls.issuer_file).read_bytes()
            )
            leaf.verify_directly_issued_by(ca)  # raises when the chain does not hold
            pinned = hashlib.sha256(leaf.public_bytes(serialization.Encoding.DER))
            if pinned.hexdigest() != host.tls.sha256:
                raise RedfishTransportError("tls", request_sent=False)
            if device.imported is not None and device.imported != leaf:
                raise RedfishTransportError("tls", request_sent=False)
            self.verified_dirs.append(directory)
            return device.view("verified")

        super().__init__(
            tmp_path,
            {"h9": device},
            der=device.weak_der,
            transport_factory=factory,
            legacy_factory=legacy_factory,
            sleeper=self.sleeps.append,
            **kwargs,
        )

    def ca_bundle(self) -> lab_ca.LabCa:
        return lab_ca.load_lab_ca(
            SecretStr(self.secrets.values[lab_ca.LAB_CA_STORE_KEY])
        )


def _post_paths(device: WeakDevice) -> list[str]:
    return [p for m, p, _b, _t in device.calls if m == "POST"]


# -- the happy path ---------------------------------------------------------------------------------


def test_a_weak_certificate_is_confirmed_replaced_and_the_setup_goes_on_to_ready(
    tmp_path: Path,
) -> None:
    device = WeakDevice(_weak_der())
    rig = WeakRig(tmp_path, device)
    result = rig.run("h9")
    host = result.hosts[0]
    assert (host.status, host.refusal, host.detail) == ("ready", None, None)
    steps = {s.step: s for s in host.steps}
    for step in (
        "weak certificate",
        "lab CA",
        "signing request",
        "certificate import",
        "certificate replaced",
        "certificate pin",
        "live",
    ):
        assert steps[step].state == "done", step
    # the operator was asked once, with the weak certificate's own fingerprint
    weak_sha = hashlib.sha256(device.weak_der).hexdigest()
    assert rig.confirmations == [
        ("h9", "192.0.2.10", cert.colon_fingerprint(weak_sha), "1a2b3c4d", 1024)
    ]
    assert any(cert.colon_fingerprint(weak_sha) in line for line in rig.lines)
    # the iLO generated the CSR through the action it listed, and the lab CA signed what it returned
    assert device.csr_params["CommonName"] == "h9-ilo"
    assert device.imported is not None
    ca = rig.ca_bundle()
    assert lab_ca.issued_by(device.imported, ca)
    key = device.imported.public_key()
    assert isinstance(key, rsa.RSAPublicKey)
    assert key.key_size == 2048
    # the overlay pins the NEW certificate and names the lab CA as its issuer
    row = rig.overlay()["hosts"][0]
    ModelIloOverlay.model_validate(rig.overlay())
    new_sha = hashlib.sha256(device.imported.public_bytes(serialization.Encoding.DER))
    assert row["tls"] == {
        "ca_file": "h9-ilo.pem",
        "sha256": new_sha.hexdigest(),
        "issuer_file": lab_ca.LAB_CA_FILE,
    }
    assert row["live"] is True
    # the connection after the reset was made verifying the lab CA (the factory raises otherwise)
    assert rig.verified_dirs
    assert Path(rig.overlay_dir, lab_ca.LAB_CA_FILE).read_text() == ca.certificate_pem
    # and waited out the iLO's reset instead of guessing a delay
    assert len([s for s in rig.sleeps if s > 0]) >= 2 + device.reset_polls


def test_the_lab_ca_key_lives_only_in_the_store_and_is_created_once(
    tmp_path: Path,
) -> None:
    device = WeakDevice(_weak_der())
    rig = WeakRig(tmp_path, device)
    result = rig.run("h9")
    assert rig.secrets.created.count(lab_ca.LAB_CA_STORE_KEY) == 1
    bundle = rig.secrets.values[lab_ca.LAB_CA_STORE_KEY]
    assert "BEGIN PRIVATE KEY" in bundle
    # no file the setup wrote, no screen line and no result field carries the CA key
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert "PRIVATE KEY" not in path.read_text(errors="ignore"), path
    shown = rig.everything_printed(result)
    assert "PRIVATE KEY" not in shown
    assert ADMIN_PASSWORD not in shown
    # a second host with a weak certificate reuses the same CA and creates no other
    second = WeakDevice(_weak_der(0x55AA55AA))
    again = WeakRig(tmp_path / "b", second, shared_secrets=rig.secrets)
    assert again.run("h9").hosts[0].status == "ready"
    assert rig.secrets.created.count(lab_ca.LAB_CA_STORE_KEY) == 1
    assert second.imported is not None
    assert lab_ca.issued_by(second.imported, rig.ca_bundle())


def test_the_legacy_connection_carries_only_the_sign_in_and_the_two_certificate_actions(
    tmp_path: Path,
) -> None:
    device = WeakDevice(_weak_der())
    WeakRig(tmp_path, device).run("h9")
    legacy_posts: list[str] = []
    # everything the device saw before it reset: reads, the sign-in, the two actions, nothing else
    for method, path, _body, _tok in device.calls:
        if method == "POST":
            legacy_posts.append(path)
        assert method in {"GET", "POST", "DELETE"}
    first_account = next(
        i for i, p in enumerate(_post_paths(device)) if p.endswith("/accounts/")
    )
    before = _post_paths(device)[:first_account]
    assert [p for p in before if "/sessions" not in p] == [GENERATE, IMPORT]
    assert device.legacy_pins == [hashlib.sha256(device.weak_der).hexdigest()]


def test_a_firmware_that_lists_the_actions_in_the_older_form_names_them_in_the_body(
    tmp_path: Path,
) -> None:
    device = WeakDevice(_weak_der(), style="legacy")
    rig = WeakRig(tmp_path, device)
    assert rig.run("h9").hosts[0].status == "ready"
    posted = [
        b
        for m, p, b, _t in device.calls
        if m == "POST" and p == HTTPS and isinstance(b, dict)
    ]
    assert [b["Action"] for b in posted] == ["GenerateCSR", "ImportCertificate"]


def test_an_ilo_that_drops_the_connection_while_it_resets_still_ends_ready(
    tmp_path: Path,
) -> None:
    device = WeakDevice(_weak_der(), drop_on_import=True)
    assert WeakRig(tmp_path, device).run("h9").hosts[0].status == "ready"


# -- the operator's confirmation is the only bootstrap trust ----------------------------------------


def test_a_declined_fingerprint_sends_nothing_and_creates_no_ca(tmp_path: Path) -> None:
    device = WeakDevice(_weak_der())
    rig = WeakRig(tmp_path, device, confirm=False)
    result = rig.run("h9")
    assert result.hosts[0].refusal is R.FINGERPRINT_UNCONFIRMED
    assert device.calls == []
    assert lab_ca.LAB_CA_STORE_KEY not in rig.secrets.values
    assert rig.verified_dirs == []
    assert not (rig.overlay_dir / "overlay.yaml").exists()


def test_a_certificate_that_is_not_the_one_confirmed_is_refused_before_any_request(
    tmp_path: Path,
) -> None:
    device = WeakDevice(_weak_der())
    rig = WeakRig(tmp_path, device)
    rig.der = _weak_der(
        0x0BADBEEF
    )  # what the fetch saw is not what the device presents
    result = rig.run("h9")
    assert result.hosts[0].refusal is R.TLS_FAILED
    assert device.calls == []
    assert device.imported is None


def test_an_ilo_reporting_another_serial_than_the_one_presented_is_refused_before_a_csr(
    tmp_path: Path,
) -> None:
    device = WeakDevice(_weak_der())
    device.reported_override = "99"
    rig = WeakRig(tmp_path, device)
    result = rig.run("h9")
    assert result.hosts[0].refusal is R.FINGERPRINT_MISMATCH
    assert device.csr_key is None
    assert device.imported is None


def test_the_legacy_scope_lets_through_only_reads_the_sign_in_and_allowed_actions() -> (
    None
):
    class Spy:
        def __init__(self) -> None:
            self.seen: list[tuple[str, str]] = []

        def request(
            self,
            method: Method,
            path: str,
            *,
            body: JsonValue = None,
            token: SecretStr | None = None,
        ) -> ModelRedfishResponse:
            self.seen.append((method, path))
            return ModelRedfishResponse(status=200)

    spy = Spy()
    scope = _LegacyScope(spy)
    scope.request("GET", "/redfish/v1/anything/")
    scope.request("POST", "/redfish/v1/sessions/", body={})
    scope.request("DELETE", "/redfish/v1/sessions/abc/")
    refused: list[tuple[Method, str]] = [
        ("POST", "/redfish/v1/accountservice/accounts/"),
        ("POST", GENERATE),  # not yet allowed
        ("PATCH", HTTPS),
        ("DELETE", "/redfish/v1/accountservice/accounts/1/"),
        ("DELETE", "/redfish/v1/sessions/"),
        ("POST", "/redfish/v1/systems/1/actions/computersystem.reset/"),
    ]
    for method, path in refused:
        with pytest.raises(RedfishTransportError):
            scope.request(method, path, body={})
    scope.allow_post(GENERATE)
    scope.request("POST", GENERATE, body={})
    assert len(spy.seen) == 4
    assert all(call not in spy.seen for call in refused[:1] + refused[2:])


# -- refusals, each with nothing replaced -----------------------------------------------------------


def test_an_ilo_that_lists_no_csr_action_is_refused_not_guessed(tmp_path: Path) -> None:
    device = WeakDevice(_weak_der(), style="none")
    rig = WeakRig(tmp_path, device)
    result = rig.run("h9")
    assert result.hosts[0].refusal is R.CSR_UNSUPPORTED
    assert "GenerateCSR" in (result.hosts[0].detail or "")
    assert [p for p in _post_paths(device) if "/sessions" not in p] == []
    assert device.imported is None


def test_a_signing_request_with_a_weak_key_is_refused_and_nothing_is_signed_or_imported(
    tmp_path: Path,
) -> None:
    device = WeakDevice(_weak_der(), csr_bits=1024)
    result = WeakRig(tmp_path, device).run("h9")
    assert result.hosts[0].refusal is R.CSR_FAILED
    assert device.imported is None


def test_a_signing_request_that_never_appears_is_refused(tmp_path: Path) -> None:
    device = WeakDevice(_weak_der(), csr_polls=10_000)
    result = WeakRig(tmp_path, device).run("h9")
    assert result.hosts[0].refusal is R.CSR_FAILED
    assert device.imported is None


def test_a_refused_import_is_reported_and_the_pin_is_not_written(
    tmp_path: Path,
) -> None:
    device = WeakDevice(_weak_der(), import_status=400)
    rig = WeakRig(tmp_path, device)
    result = rig.run("h9")
    assert result.hosts[0].refusal is R.CERTIFICATE_IMPORT_FAILED
    assert "iLO.0.10.BadCertificate" in (result.hosts[0].detail or "")
    assert not (rig.overlay_dir / "overlay.yaml").exists()


def test_an_ilo_that_does_not_come_back_with_the_new_certificate_times_out(
    tmp_path: Path,
) -> None:
    device = WeakDevice(_weak_der(), reset_polls=10_000)
    rig = WeakRig(tmp_path, device)
    result = rig.run("h9")
    assert result.hosts[0].refusal is R.ILO_RESET_TIMEOUT
    assert len([s for s in rig.sleeps if s > 0]) >= RESET_POLLS
    assert not (rig.overlay_dir / "overlay.yaml").exists()


def test_a_stored_lab_ca_that_is_not_a_ca_is_refused_and_never_replaced(
    tmp_path: Path,
) -> None:
    device = WeakDevice(_weak_der())
    rig = WeakRig(
        tmp_path, device, secrets_values={lab_ca.LAB_CA_STORE_KEY: "not a certificate"}
    )
    result = rig.run("h9")
    assert result.hosts[0].refusal is R.LAB_CA_UNAVAILABLE
    assert rig.secrets.created.count(lab_ca.LAB_CA_STORE_KEY) == 0
    assert device.imported is None


# -- a rerun after the replacement -------------------------------------------------------------------


def test_a_rerun_after_the_replacement_makes_no_csr_and_trusts_only_the_lab_ca(
    tmp_path: Path,
) -> None:
    device = WeakDevice(_weak_der())
    rig = WeakRig(tmp_path, device)
    assert rig.run("h9").hosts[0].status == "ready"
    assert device.imported is not None
    generated = len(_post_paths(device))
    rig.der = device.imported.public_bytes(serialization.Encoding.DER)
    rig.confirmations.clear()
    rig._password = None
    second = rig.run("h9")
    assert second.hosts[0].status == "ready"
    assert rig.confirmations == []
    assert device.csr_gets > 0
    assert not [p for p in _post_paths(device)[generated:] if p in (GENERATE, IMPORT)]
    assert rig.overlay()["hosts"][0]["tls"]["issuer_file"] == lab_ca.LAB_CA_FILE


def test_a_certificate_signed_by_someone_else_is_refused_not_trusted(
    tmp_path: Path,
) -> None:
    device = WeakDevice(_weak_der())
    device.state = "new"
    rig = WeakRig(tmp_path, device)
    other = lab_ca.create_lab_ca(NOW)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "x")]))
        .sign(key, hashes.SHA256())
    )
    foreign = lab_ca.sign_csr(other, csr, address="192.0.2.10", now=NOW)
    rig.der = foreign.public_bytes(serialization.Encoding.DER)
    # no lab CA in the store at all: the foreign issuer cannot be trusted
    assert rig.run("h9").hosts[0].refusal is R.ISSUER_UNKNOWN
    # a lab CA in the store that did not sign it: still refused
    mine = lab_ca.create_lab_ca(NOW)
    rig.secrets.values[lab_ca.LAB_CA_STORE_KEY] = mine.bundle().get_secret_value()
    assert rig.run("h9").hosts[0].refusal is R.ISSUER_UNKNOWN


def test_a_modern_rsa_certificate_is_never_replaced(tmp_path: Path) -> None:
    der, _ = _self_signed(2048, "ilo-test", 0x1F)
    assert not cert.is_weak(cert.presented(der))
    rig = Rig(
        tmp_path,
        {"h9": SetupDevice("gen9", reported_serial="1f")},
        der=der,
    )
    assert rig.run("h9").hosts[0].status == "ready"
    # a 2048-bit certificate takes the ordinary path: no CA, no CSR, no replacement; being self-signed
    # and unpinned it is confirmed once, before any password is sent
    assert len(rig.confirmations) == 1
    assert lab_ca.LAB_CA_STORE_KEY not in rig.secrets.values


# -- units -------------------------------------------------------------------------------------------


def test_the_weakness_threshold_and_the_fingerprint_form() -> None:
    assert cert.is_weak(cert.presented(_weak_der()))
    assert not cert.is_weak(cert.presented(_self_signed(2048, "x", 1)[0]))
    assert cert.colon_fingerprint("c08d" + "00" * 30) == "C0:8D:" + ":".join(
        ["00"] * 30
    )


def test_actions_are_found_by_name_in_either_form_and_never_followed_off_the_api() -> (
    None
):
    redfish = {"Actions": {"#HpHttpsCert.GenerateCSR": {"target": GENERATE}}}
    found = cert.find_action(redfish, HTTPS, "GenerateCSR")
    assert found is not None
    assert (found.target, found.named_in_body) == (GENERATE, False)
    nested = {
        "Oem": {
            "Hpe": {"Actions": {"#HpeHttpsCert.ImportCertificate": {"target": IMPORT}}}
        }
    }
    assert cert.find_action(nested, HTTPS, "ImportCertificate") is not None
    old = {"AvailableActions": [{"Action": "GenerateCSR"}]}
    legacy = cert.find_action(old, HTTPS, "GenerateCSR")
    assert legacy is not None
    assert legacy.body("GenerateCSR", {"A": "b"}) == {"Action": "GenerateCSR", "A": "b"}
    off = {"Actions": {"#X.GenerateCSR": {"target": "https://elsewhere.example/x"}}}
    assert cert.find_action(off, HTTPS, "GenerateCSR") is None
    assert cert.find_action({"Actions": {}}, HTTPS, "GenerateCSR") is None
    assert cert.action_names(redfish) == ("generatecsr",)


def test_the_lab_ca_signs_a_server_certificate_and_refuses_a_bad_request() -> None:
    ca = lab_ca.create_lab_ca(NOW)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "h9-ilo")]))
        .sign(key, hashes.SHA256())
    )
    pem = csr.public_bytes(serialization.Encoding.PEM).decode()
    leaf = lab_ca.sign_csr(ca, lab_ca.read_csr(pem), address="192.0.2.10", now=NOW)
    assert lab_ca.issued_by(leaf, ca)
    assert not lab_ca.issued_by(leaf, lab_ca.create_lab_ca(NOW))
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert [str(v) for v in san.value.get_values_for_type(x509.IPAddress)] == [
        "192.0.2.10"
    ]
    with pytest.raises(lab_ca.LabCaError):
        lab_ca.read_csr("not a request")
    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    weak_csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "h9-ilo")]))
        .sign(weak, hashes.SHA256())
    )
    with pytest.raises(lab_ca.LabCaError):
        lab_ca.read_csr(weak_csr.public_bytes(serialization.Encoding.PEM).decode())
    loaded = lab_ca.load_lab_ca(ca.bundle())
    assert loaded.certificate == ca.certificate
    with pytest.raises(lab_ca.LabCaError):
        lab_ca.load_lab_ca(
            type(ca.bundle())(
                ca.certificate_pem
                + lab_ca.create_lab_ca(NOW)
                .bundle()
                .get_secret_value()
                .split("-----END CERTIFICATE-----\n")[1]
            )
        )


# -- the real transports against a real TLS server with an RSA 1024-bit certificate ------------------


class _Server:
    """A local TLS 1.2 server that, like the iLO, presents an RSA certificate."""

    def __init__(
        self, tmp: Path, der_and_key: tuple[bytes, bytes], chain: bytes | None = None
    ) -> None:
        der, key_pem = der_and_key
        cert_pem = ssl.DER_cert_to_PEM_cert(der).encode()
        (tmp / "srv.pem").write_bytes(cert_pem + (chain or b""))
        (tmp / "srv.key").write_bytes(key_pem)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        context.set_ciphers("DEFAULT:@SECLEVEL=0")
        context.load_cert_chain(tmp / "srv.pem", tmp / "srv.key")
        self.received: list[bytes] = []
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(8)
        self._socket.settimeout(5)
        self.port = self._socket.getsockname()[1]
        self._context = context
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                raw, _ = self._socket.accept()
            except OSError:
                return
            try:
                with self._context.wrap_socket(raw, server_side=True) as conn:
                    conn.settimeout(2)
                    data = conn.recv(4096)
                    self.received.append(data)
                    if data:
                        body = b'{"ok": true}'
                        conn.sendall(
                            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                            b"Connection: close\r\nContent-Length: "
                            + str(len(body)).encode()
                            + b"\r\n\r\n"
                            + body
                        )
            except (OSError, ssl.SSLError):
                continue

    def close(self) -> None:
        self._stop = True
        self._socket.close()


@pytest.fixture
def weak_server(tmp_path: Path) -> Iterator[tuple[_Server, bytes]]:
    material = _self_signed(LEGACY_ILO_KEY_BITS, "ILOUSE443DE5W", 0x1A2B3C4D)
    server = _Server(tmp_path, material)
    yield server, material[0]
    server.close()


def _host(
    port: int, sha256: str, ca_file: str = "x.pem", issuer: str | None = None
) -> ModelIloHost:
    tls: dict[str, str] = {"ca_file": ca_file, "sha256": sha256}
    if issuer:
        tls["issuer_file"] = issuer
    return ModelIloHost.model_validate(
        {
            "name": "h9",
            "generation": "gen9",
            "address": "127.0.0.1",
            "port": port,
            "expected_product": "x",
            "tls": tls,
        }
    )


def test_the_certificate_fetch_reads_a_certificate_the_default_security_level_refuses(
    weak_server: tuple[_Server, bytes],
) -> None:
    server, der = weak_server
    assert fetch_certificate_der("127.0.0.1", server.port) == der


def test_the_legacy_transport_talks_only_after_an_exact_pin_match(
    weak_server: tuple[_Server, bytes], tmp_path: Path
) -> None:
    server, der = weak_server
    pin = hashlib.sha256(der).hexdigest()
    transport = build_legacy_pinned_transport(_host(server.port, pin), tmp_path)
    response = transport.request("GET", "/redfish/v1/")
    assert response.status == 200
    assert response.body == {"ok": True}


def test_the_legacy_transport_refuses_a_different_fingerprint_before_sending_a_byte(
    weak_server: tuple[_Server, bytes], tmp_path: Path
) -> None:
    server, der = weak_server
    flipped = "0" * 64 if hashlib.sha256(der).hexdigest() != "0" * 64 else "1" * 64
    transport = build_legacy_pinned_transport(_host(server.port, flipped), tmp_path)
    with pytest.raises(RedfishTransportError) as excinfo:
        transport.request("POST", "/redfish/v1/sessions/", body={"UserName": "x"})
    assert excinfo.value.kind == "tls"
    assert excinfo.value.request_sent is False
    server.close()
    assert server.received in ([], [b""])


def test_the_normal_transport_verifies_the_lab_ca_chain_and_the_pin(
    tmp_path: Path,
) -> None:
    ca = lab_ca.create_lab_ca(NOW)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "h9-ilo")]))
        .sign(key, hashes.SHA256())
    )
    leaf = lab_ca.sign_csr(ca, csr, address="127.0.0.1", now=dt.datetime.now(dt.UTC))
    leaf_der = leaf.public_bytes(serialization.Encoding.DER)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "leaf.pem").write_text(ssl.DER_cert_to_PEM_cert(leaf_der))
    (overlay / "ca.pem").write_text(ca.certificate_pem)
    other = lab_ca.create_lab_ca(NOW)
    (overlay / "other-ca.pem").write_text(other.certificate_pem)
    server = _Server(tmp_path, (leaf_der, key_pem))
    try:
        pin = hashlib.sha256(leaf_der).hexdigest()
        good = build_pinned_transport(
            _host(server.port, pin, "leaf.pem", "ca.pem"), overlay
        )
        assert good.request("GET", "/redfish/v1/").status == 200
        # control: the same leaf and pin, but a CA that did not sign it, does not verify
        wrong = build_pinned_transport(
            _host(server.port, pin, "leaf.pem", "other-ca.pem"), overlay
        )
        with pytest.raises(RedfishTransportError) as excinfo:
            wrong.request("GET", "/redfish/v1/")
        assert excinfo.value.kind == "tls"
        # control: the right CA but another pin refuses
        other_pin = _host(server.port, "f" * 64, "leaf.pem", "ca.pem")
        pinned_elsewhere = PinnedHttpsTransport(
            other_pin,
            pinned_context(_host(server.port, pin, "leaf.pem", "ca.pem"), overlay),
        )
        with pytest.raises(RedfishTransportError):
            pinned_elsewhere.request("GET", "/redfish/v1/")
    finally:
        server.close()


def test_a_pin_file_that_does_not_hash_to_the_pin_is_refused_with_an_issuer_too(
    tmp_path: Path,
) -> None:
    ca = lab_ca.create_lab_ca(NOW)
    (tmp_path / "leaf.pem").write_text(ca.certificate_pem)
    (tmp_path / "ca.pem").write_text(ca.certificate_pem)
    with pytest.raises(RedfishTransportError):
        build_pinned_transport(_host(1, "a" * 64, "leaf.pem", "ca.pem"), tmp_path)
