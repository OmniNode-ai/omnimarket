"""Adapters, models, redaction and the pinned TLS transport (OMN-21835). Loopback only, no real iLO."""

from __future__ import annotations

import hashlib
import json
import ssl
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pydantic import SecretStr, ValidationError

from omnimarket.enums.enum_secret_source import EnumSecretSource
from omnimarket.nodes.node_ilo_redfish_effect.handlers.credential_store import (
    StoreCredentialSource,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.handler_ilo_redfish import (
    CONTRACT_PATH,
    load_credential_key_template,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.journal_file import (
    FileActionJournal,
    JournalUnreadableError,
    LedgerApprovalSource,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.protocols import (
    CredentialSourceNotStoreError,
    RedfishTransportError,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.redaction import (
    MASK,
    pipe_free,
    redact_json,
    redact_text,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.redfish_client import (
    IloRefusalError,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.transport_pinned_https import (
    PinnedHttpsTransport,
    build_pinned_transport,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.enum_ilo_redfish import (
    EnumIloJournalStatus,
    EnumIloVerb,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_overlay import (
    ModelIloHost,
    ModelIloOverlay,
    credential_key,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_redfish_request import (
    ModelIloRedfishRequest,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_redfish_result import (
    ModelIloJournalRecord,
)
from tests.nodes.node_ilo_redfish_effect.ilo_fakes import dig

pytestmark = pytest.mark.unit


# -- redaction -------------------------------------------------------------------------------


def test_redaction_masks_sensitive_keys_license_keys_headers_and_url_userinfo() -> None:
    cleaned = redact_json(
        {
            "UserName": "reader",
            "Password": "hunter2",
            "X-Auth-Token": "abc",
            "LicenseKey": "x",
            "LicenseType": "Perpetual",
            "note": "key ABCDE-12345-FGHIJ-67890-KLMNO and authorization: Bearer zzz at http://u:p@h/x",
            "nested": [{"api_key": "k"}],
        }
    )
    assert cleaned["Password"] == MASK
    assert cleaned["X-Auth-Token"] == MASK
    assert cleaned["LicenseKey"] == MASK
    assert cleaned["LicenseType"] == "Perpetual"
    assert dig(cleaned, "nested") == [{"api_key": MASK}]
    text = json.dumps(cleaned)
    for secret in ("hunter2", "ABCDE-12345", "Bearer zzz", "u:p@"):
        assert secret not in text


def test_redaction_bounds_text_and_ledger_rows_are_one_line() -> None:
    assert len(redact_text("x" * 5000)) < 2100
    assert pipe_free("a | b\nc") == "a / b c"


# -- overlay and request models --------------------------------------------------------------


def _host(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "h9",
        "generation": "gen9",
        "address": "192.0.2.9",
        "expected_product": "DL360 Gen9",
        "tls": {"ca_file": "certs/a.pem", "sha256": "a" * 64},
    }
    base.update(over)
    return base


def test_overlay_is_inert_by_default_and_rejects_path_escapes_and_duplicates() -> None:
    row = ModelIloHost.model_validate(_host())
    assert row.live is False
    assert row.expected_serial is None
    for bad in ("../x.pem", "/etc/ssl/x.pem", "a/../../x.pem"):
        with pytest.raises(ValidationError):
            ModelIloHost.model_validate(_host(tls={"ca_file": bad, "sha256": "a" * 64}))
    with pytest.raises(ValidationError):
        ModelIloHost.model_validate(_host(tls={"ca_file": "a.pem", "sha256": "ABC"}))
    with pytest.raises(ValidationError):
        ModelIloOverlay.model_validate(
            {"hosts": [_host(), _host()], "journal_dir": "j"}
        )


def test_overlay_rejects_unknown_fields_so_a_credential_value_has_no_place_to_sit() -> (
    None
):
    with pytest.raises(ValidationError):
        ModelIloHost.model_validate(_host(password="hunter2"))


def test_credential_key_template_yields_store_key_names() -> None:
    assert (
        credential_key(
            "ILO_{HOST}_{ROLE}_{FIELD}", host="h-051", role="READ", field="USERNAME"
        )
        == "ILO_H_051_READ_USERNAME"
    )
    assert load_credential_key_template() == "ILO_{HOST}_{ROLE}_{FIELD}"


def test_request_parameters_must_match_the_verb() -> None:
    base = {"host": "h9", "requested_by": "lane-a"}
    ModelIloRedfishRequest.model_validate({**base, "verb": "power-status"})
    for bad in (
        {"verb": "power"},  # power needs an action
        {"verb": "power-status", "power_action": "on"},  # a read takes no action
        {
            "verb": "power-status",
            "approval_ref": "2026-11-02T11:00:00Z",
        },  # a read takes no approval
        {"verb": "virtual-media", "media_action": "insert"},  # insert needs an image
        {
            "verb": "virtual-media",
            "media_action": "eject",
            "image_url": "http://192.0.2.10/a.iso",
        },
        {"verb": "power", "power_action": "nmi"},  # Nmi is not offered
        {"verb": "format-disk"},  # no storage verb exists
    ):
        with pytest.raises(ValidationError):
            ModelIloRedfishRequest.model_validate({**base, **bad})


def test_no_verb_can_reach_storage_accounts_licenses_firmware_or_log_clearing() -> None:
    values = {v.value for v in EnumIloVerb}
    assert values == {
        "discover",
        "identity",
        "inventory",
        "health",
        "logs",
        "power-status",
        "power",
        "virtual-media",
        "boot-once",
    }


def test_contract_declares_no_bus_and_no_secret_values() -> None:
    raw = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert "event_bus" not in raw
    assert "handler_routing" not in raw
    assert raw["node_type"] == "effect"
    assert (
        "secrets" not in raw
    )  # key names come from the template and the overlay, values from the store


# -- credential source -----------------------------------------------------------------------


def test_credentials_come_only_from_the_lane_secret_store() -> None:
    ok = StoreCredentialSource(
        lambda _: (SecretStr("v"), EnumSecretSource.LANE_SECRET_STORE)
    )
    assert ok.get("ILO_H9_READ_PASSWORD") is not None
    env = StoreCredentialSource(
        lambda _: (SecretStr("v"), EnumSecretSource.ENVIRONMENT)
    )
    with pytest.raises(CredentialSourceNotStoreError):
        env.get("ILO_H9_READ_PASSWORD")
    assert StoreCredentialSource(lambda _: (None, None)).get("K") is None

    def boom(key: str) -> tuple[SecretStr | None, EnumSecretSource | None]:
        raise RuntimeError("connection string with secret=abc")

    with pytest.raises(IloRefusalError) as caught:
        StoreCredentialSource(boom).get("K")
    assert "secret=abc" not in (caught.value.detail or "")


# -- journal and ledger approval source ------------------------------------------------------


def _record(key: str, status: EnumIloJournalStatus) -> ModelIloJournalRecord:
    return ModelIloJournalRecord(
        key=key,
        host="h9",
        action="power:on",
        status=status,
        at=datetime(2026, 11, 2, tzinfo=UTC),
    )


def test_file_journal_round_trips_per_key_and_is_private(tmp_path: Path) -> None:
    journal = FileActionJournal(tmp_path / "journal")
    journal.append(_record("k1", EnumIloJournalStatus.INTENT))
    journal.append(_record("k2", EnumIloJournalStatus.INTENT))
    journal.append(_record("k1", EnumIloJournalStatus.VERIFIED))
    assert [r.status for r in journal.lookup("h9", "k1")] == [
        EnumIloJournalStatus.INTENT,
        EnumIloJournalStatus.VERIFIED,
    ]
    assert journal.lookup("h9", "none") == []
    assert journal.lookup("other", "k1") == []
    assert (tmp_path / "journal" / "h9.jsonl").stat().st_mode & 0o077 == 0


def test_an_unreadable_journal_line_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "h9.jsonl").write_text("not json\n", encoding="utf-8")
    with pytest.raises(JournalUnreadableError):
        FileActionJournal(tmp_path).lookup("h9", "k")


def test_ledger_approval_source_reads_an_operator_consent_row(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        "2026-11-02T10:00:00Z | STATUS | lane=x | nothing\n"
        '2026-11-02T11:00:00Z | OPERATOR-CONSENT | lane=orchestrator-x | "go" | '
        "APPROVED SCOPE: ilo-redfish host=h9 action=power:on | OUT OF SCOPE: all else | durable\n"
        '2026-11-02T11:05:00Z | OPERATOR-CONSENT | lane=orchestrator-x | "go" | APPROVED SCOPE: only this\n',
        encoding="utf-8",
    )
    source = LedgerApprovalSource(ledger)
    row = source.find("2026-11-02T11:00:00Z")
    assert row is not None
    assert row.approved_by == "operator"
    assert row.lane == "orchestrator-x"
    assert "ilo-redfish" in row.approved_scope
    assert row.out_of_scope == "all else"
    assert (
        source.find("2026-11-02T11:05:00Z") is None
    )  # no OUT OF SCOPE cell: not a valid consent
    assert source.find("2026-11-02T10:00:00Z") is None  # not a consent row
    assert source.find("2030-01-01T00:00:00Z") is None


# -- pinned TLS ------------------------------------------------------------------------------


def _self_signed(common_name: str) -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ),
    )


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps({"RedfishVersion": "1.0.0"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return None


@pytest.fixture
def tls_server(tmp_path: Path) -> Iterator[tuple[int, bytes]]:
    """A loopback HTTPS server with a self-signed certificate. Yields (port, its PEM)."""
    pem, key = _self_signed("ilo-test")
    cert_file, key_file = tmp_path / "srv.pem", tmp_path / "srv.key"
    cert_file.write_bytes(pem)
    key_file.write_bytes(key)
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_file, key_file)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], pem
    finally:
        server.shutdown()
        server.server_close()


def _pinned_host(port: int, pem: bytes, *, pin: str | None = None) -> ModelIloHost:
    der = ssl.PEM_cert_to_DER_cert(pem.decode())
    return ModelIloHost.model_validate(
        _host(
            address="127.0.0.1",
            port=port,
            tls={
                "ca_file": "pinned.pem",
                "sha256": pin or hashlib.sha256(der).hexdigest(),
            },
        )
    )


def test_the_pinned_certificate_is_trusted_and_verification_stays_on(
    tls_server: tuple[int, bytes], tmp_path: Path
) -> None:
    port, pem = tls_server
    (tmp_path / "pinned.pem").write_bytes(pem)
    transport = build_pinned_transport(_pinned_host(port, pem), tmp_path, timeout_s=5)
    response = transport.request("GET", "/redfish/v1/")
    assert response.status == 200
    assert response.body == {"RedfishVersion": "1.0.0"}
    context = transport._context
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_a_server_presenting_another_certificate_is_refused(
    tls_server: tuple[int, bytes], tmp_path: Path
) -> None:
    port, _ = tls_server
    other_pem, _ = _self_signed("someone-else")
    (tmp_path / "pinned.pem").write_bytes(other_pem)
    transport = build_pinned_transport(
        _pinned_host(port, other_pem), tmp_path, timeout_s=5
    )
    with pytest.raises(RedfishTransportError) as caught:
        transport.request("GET", "/redfish/v1/")
    assert caught.value.kind == "tls"
    assert caught.value.request_sent is False


def test_a_pem_that_does_not_hash_to_the_pin_is_refused_at_load(
    tls_server: tuple[int, bytes], tmp_path: Path
) -> None:
    port, pem = tls_server
    (tmp_path / "pinned.pem").write_bytes(pem)
    with pytest.raises(RedfishTransportError) as caught:
        build_pinned_transport(_pinned_host(port, pem, pin="b" * 64), tmp_path)
    assert caught.value.kind == "tls"


def test_a_ca_file_escaping_the_overlay_directory_is_refused(tmp_path: Path) -> None:
    pem, _ = _self_signed("x")
    outside = tmp_path / "outside.pem"
    outside.write_bytes(pem)
    overlay_dir = tmp_path / "overlay"
    overlay_dir.mkdir()
    (overlay_dir / "link.pem").symlink_to(outside)
    host = _pinned_host(1, pem)
    host = host.model_copy(
        update={"tls": host.tls.model_copy(update={"ca_file": "link.pem"})}
    )
    with pytest.raises(RedfishTransportError):
        build_pinned_transport(host, overlay_dir)


def test_the_transport_never_leaves_the_redfish_tree(
    tls_server: tuple[int, bytes], tmp_path: Path
) -> None:
    port, pem = tls_server
    (tmp_path / "pinned.pem").write_bytes(pem)
    transport: PinnedHttpsTransport = build_pinned_transport(
        _pinned_host(port, pem), tmp_path, timeout_s=5
    )
    for path in (
        "/etc/passwd",
        "https://203.0.113.9/redfish/v1/",
        "/redfish/v1/\r\nX: y",
    ):
        with pytest.raises(RedfishTransportError) as caught:
            transport.request("GET", path)
        assert caught.value.kind == "protocol"
        assert caught.value.request_sent is False


def test_a_missing_ledger_file_is_no_approval_not_a_crash(tmp_path: Path) -> None:
    assert (
        LedgerApprovalSource(tmp_path / "absent.md").find("2026-11-02T11:00:00Z")
        is None
    )
