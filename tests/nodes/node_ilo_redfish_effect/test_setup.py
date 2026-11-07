"""The iLO setup verb over a scripted Redfish transport and a dict-backed secret store (OMN-22870).

Nothing here touches a network. The Redfish trees extend the synthetic fakes of the node's other tests;
the certificate is a real self-signed one generated per test run so the presented-versus-reported
comparison runs on real DER.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
from pathlib import Path
from typing import Literal

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pydantic import JsonValue, SecretStr

from omnimarket.nodes.node_ilo_redfish_effect.handlers.handler_ilo_setup import (
    HandlerIloSetup,
    generate_password,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.protocols import (
    ModelRedfishResponse,
    ProtocolRedfishTransport,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.setup_ports import (
    SetupStoreError,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_overlay import (
    ModelIloHost,
    ModelIloOverlay,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_setup import (
    EnumIloSetupRefusal as R,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_setup import (
    ModelIloSetupRequest,
    ModelIloSetupResult,
)
from tests.nodes.node_ilo_redfish_effect.ilo_fakes import (
    GEN9_SERIAL,
    FakeIloTransport,
    Json,
    _ref,
    ilo_resources,
    no_sleep,
)

pytestmark = pytest.mark.unit

TEMPLATE = "ILO_{HOST}_{ROLE}_{FIELD}"
ADMIN_PASSWORD = "admin-secret-value-1"
NOW = dt.datetime(2026, 11, 7, 13, 0, tzinfo=dt.UTC)
ACCOUNTS = "/redfish/v1/accountservice/accounts/"
READ_PRIVS = {"LoginPriv"}
WRITE_PRIVS = {"LoginPriv", "VirtualMediaPriv", "VirtualPowerAndResetPriv"}
ALL = (
    "LoginPriv",
    "RemoteConsolePriv",
    "UserConfigPriv",
    "VirtualMediaPriv",
    "VirtualPowerAndResetPriv",
    "iLOConfigPriv",
)


def make_certificate(common_name: str) -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(0xA1B2C3D4E5F60789)
        .not_valid_before(NOW - dt.timedelta(days=1))
        .not_valid_after(NOW + dt.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.DER)


class SetupDevice(FakeIloTransport):
    """A synthetic iLO that also serves accounts and its own HTTPS certificate report."""

    def __init__(
        self,
        generation: Literal["gen8", "gen9"],
        *,
        reported_serial: str | None = "A1:B2:C3:D4:E5:F6:07:89",
        reported_sha256: str | None = None,
        accounts: dict[str, set[str]] | None = None,
    ) -> None:
        resources = ilo_resources(generation)
        root = resources["/redfish/v1/"]
        root["AccountService"] = _ref("/redfish/v1/accountservice/")
        resources["/redfish/v1/accountservice/"] = {"Accounts": _ref(ACCOUNTS)}
        resources[ACCOUNTS] = {"Members": []}
        manager = resources["/redfish/v1/managers/1/"]
        hp = manager["Oem"]["Hp"]  # type: ignore[index]
        hp["links"]["SecurityService"] = _ref("/redfish/v1/managers/1/securityservice/")  # type: ignore[index]
        resources["/redfish/v1/managers/1/securityservice/"] = {
            "links": {
                "HttpsCert": _ref("/redfish/v1/managers/1/securityservice/httpscert/")
            }
        }
        info: Json = {"Subject": "CN=ilo-test"}
        if reported_serial is not None:
            info["SerialNumber"] = reported_serial
        if reported_sha256 is not None:
            info["SHA256Fingerprint"] = reported_sha256
        resources["/redfish/v1/managers/1/securityservice/httpscert/"] = {
            "X509CertificateInformation": info
        }
        super().__init__(resources, accept_users=frozenset({"Administrator"}))
        self.account_posts: list[Json] = []
        for user, privs in (accounts or {}).items():
            self._add_account(user, privs)

    def _add_account(self, user: str, privs: set[str]) -> None:
        members = self.resources[ACCOUNTS]["Members"]
        assert isinstance(members, list)
        path = f"{ACCOUNTS}{len(members) + 1}/"
        members.append(_ref(path))
        self.resources[path] = {
            "UserName": user,
            "Oem": {"Hp": {"Privileges": {p: p in privs for p in ALL}}},
        }
        self.accept_users = self.accept_users | {user}

    def _write(self, method: str, path: str, body: JsonValue) -> ModelRedfishResponse:
        if method == "POST" and path == ACCOUNTS:
            assert isinstance(body, dict)
            self.account_posts.append(copy.deepcopy(body))
            hp = body["Oem"]["Hp"]  # type: ignore[index]
            granted = {k for k, v in hp["Privileges"].items() if v}  # type: ignore[index]
            self._add_account(str(body["UserName"]), granted)
            return ModelRedfishResponse(status=201, body={})
        return super()._write(method, path, body)


class DictSecrets:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.created: list[str] = []
        self.read: list[str] = []
        self.down = False

    def names(self) -> frozenset[str]:
        if self.down:
            raise SetupStoreError("the lab secret store did not answer")
        return frozenset(self.values)

    def get(self, key: str) -> SecretStr | None:
        self.read.append(key)
        value = self.values.get(key)
        return SecretStr(value) if value is not None else None

    def create(self, key: str, value: SecretStr) -> bool:
        if key in self.values:
            return False
        self.values[key] = value.get_secret_value()
        self.created.append(key)
        return True


class Rig:
    def __init__(
        self,
        tmp_path: Path,
        devices: dict[str, SetupDevice],
        *,
        secrets_values: dict[str, str] | None = None,
        table: dict[str, str] | None = None,
        password: str | None = ADMIN_PASSWORD,
        typed_address: str | None = None,
        reader_ready: bool | None = True,
    ) -> None:
        self.devices = devices
        self.secrets = DictSecrets(secrets_values)
        self.der = make_certificate("ilo-test")
        self.lines: list[str] = []
        self.password_prompts = 0
        self.fetches: list[tuple[str, int]] = []
        self.overlay_dir = tmp_path / "overlays" / "node_ilo_redfish_effect"
        self.journal_dir = tmp_path / "journal"
        self.reader_ready = reader_ready
        self._password = password
        self._typed = typed_address

        def prompt_password(user: str) -> SecretStr | None:
            self.password_prompts += 1
            return SecretStr(self._password) if self._password else None

        def factory(host: ModelIloHost, directory: Path) -> ProtocolRedfishTransport:
            del directory
            return self.devices[host.name]

        def fetch(address: str, port: int) -> bytes:
            self.fetches.append((address, port))
            return self.der

        self.handler = HandlerIloSetup(
            secrets_port=self.secrets,
            fetch_certificate=fetch,
            transport_factory=factory,
            prompt_admin_password=prompt_password,
            prompt_address=lambda _host: self._typed,
            host_table_addresses=table
            if table is not None
            else {n: f"192.0.2.{i + 10}" for i, n in enumerate(devices)},
            overlay_dir=self.overlay_dir,
            journal_dir=self.journal_dir,
            credential_key_template=TEMPLATE,
            node_reader=(
                None
                if reader_ready is None
                else (lambda _key: object() if self.reader_ready else None)
            ),
            clock=lambda: NOW,
            sleeper=no_sleep,
            emit=self.lines.append,
            password_generator=generate_password,
        )

    def run(self, *hosts: str, **kwargs: object) -> ModelIloSetupResult:
        return self.handler.handle(
            ModelIloSetupRequest(hosts=hosts or tuple(self.devices), **kwargs)  # type: ignore[arg-type]
        )

    def overlay(self) -> dict[str, object]:
        return yaml.safe_load((self.overlay_dir / "overlay.yaml").read_text())

    def everything_printed(self, result: ModelIloSetupResult) -> str:
        return "\n".join(self.lines) + result.model_dump_json()


def test_one_run_sets_up_two_hosts_and_asks_for_the_password_once(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, {"h8": SetupDevice("gen8"), "h9": SetupDevice("gen9")})
    result = rig.run()
    assert [h.status for h in result.hosts] == ["ready", "ready"]
    assert rig.password_prompts == 1
    document = rig.overlay()
    ModelIloOverlay.model_validate(document)
    rows = {r["name"]: r for r in document["hosts"]}  # type: ignore[index, union-attr]
    assert rows["h8"]["generation"] == "gen8"
    assert rows["h9"]["generation"] == "gen9"
    assert rows["h9"]["live"] is True
    assert rows["h9"]["expected_serial"] == GEN9_SERIAL
    assert rows["h9"]["expected_product"] == "ProLiant DL360 Gen9"
    assert rows["h9"]["address"].startswith("192.0.2.")
    assert rows["h9"]["tls"]["sha256"] == hashlib.sha256(rig.der).hexdigest()
    assert (
        (rig.overlay_dir / "h9-ilo.pem")
        .read_text()
        .startswith("-----BEGIN CERTIFICATE")
    )
    assert document["journal_dir"] == str(rig.journal_dir)
    assert sorted(rig.secrets.created) == sorted(
        f"ILO_H{n}_{role}_{field}"
        for n in (8, 9)
        for role in ("READ", "WRITE")
        for field in ("USERNAME", "PASSWORD")
    )


def test_accounts_are_least_privilege_and_no_password_is_ever_shown(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, {"h9": SetupDevice("gen9")})
    result = rig.run("h9")
    posts = rig.devices["h9"].account_posts
    assert len(posts) == 2
    granted = {
        str(p["UserName"]): {k for k, v in p["Oem"]["Hp"]["Privileges"].items() if v}  # type: ignore[index]
        for p in posts
    }
    assert granted == {"onex-ilo-read": READ_PRIVS, "onex-ilo-write": WRITE_PRIVS}
    shown = rig.everything_printed(result)
    secrets_in_store = [
        v for k, v in rig.secrets.values.items() if k.endswith("PASSWORD")
    ]
    assert len(secrets_in_store) == 2
    for value in [*secrets_in_store, ADMIN_PASSWORD]:
        assert value not in shown
    # the stored password is the one the iLO was given
    by_user = {str(p["UserName"]): p["Password"] for p in posts}
    assert by_user["onex-ilo-read"] == rig.secrets.values["ILO_H9_READ_PASSWORD"]
    assert by_user["onex-ilo-write"] == rig.secrets.values["ILO_H9_WRITE_PASSWORD"]


def test_the_first_read_records_the_serial_and_runs_discover_and_identity(
    tmp_path: Path,
) -> None:
    device = SetupDevice("gen9")
    rig = Rig(tmp_path, {"h9": device})
    result = rig.run("h9")
    steps = {s.step: s for s in result.hosts[0].steps}
    assert steps["discover"].state == "done"
    assert GEN9_SERIAL in steps["identity"].note
    assert result.hosts[0].node_reader_ready is True
    assert rig.overlay()["hosts"][0]["expected_serial"] == GEN9_SERIAL  # type: ignore[index]


def test_a_fingerprint_mismatch_refuses_before_anything_is_created_or_written(
    tmp_path: Path,
) -> None:
    device = SetupDevice("gen9", reported_serial="00:11:22:33")
    rig = Rig(tmp_path, {"h9": device})
    result = rig.run("h9")
    assert result.hosts[0].status == "refused"
    assert result.hosts[0].refusal is R.FINGERPRINT_MISMATCH
    assert device.account_posts == []
    assert rig.secrets.created == []
    assert not (rig.overlay_dir / "overlay.yaml").exists()
    assert not (rig.overlay_dir / "h9-ilo.pem").exists()
    assert any("presented on the connection" in line for line in rig.lines)


def test_a_reported_sha256_that_differs_refuses_even_when_the_serial_agrees(
    tmp_path: Path,
) -> None:
    device = SetupDevice("gen9", reported_sha256="ab" * 32)
    rig = Rig(tmp_path, {"h9": device})
    result = rig.run("h9")
    assert result.hosts[0].refusal is R.FINGERPRINT_MISMATCH
    assert device.account_posts == []


def test_a_matching_reported_sha256_is_accepted(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"h9": SetupDevice("gen9")})
    digest = hashlib.sha256(rig.der).hexdigest()
    rig.devices["h9"] = SetupDevice("gen9", reported_sha256=digest.upper())
    assert rig.run("h9").hosts[0].status == "ready"


def test_an_ilo_that_reports_no_certificate_serial_is_not_trusted(
    tmp_path: Path,
) -> None:
    device = SetupDevice("gen9", reported_serial=None)
    rig = Rig(tmp_path, {"h9": device})
    result = rig.run("h9")
    assert result.hosts[0].refusal is R.FINGERPRINT_UNCONFIRMED
    assert device.account_posts == []
    assert not (rig.overlay_dir / "overlay.yaml").exists()


def test_a_rerun_reuses_the_accounts_and_creates_nothing_again(tmp_path: Path) -> None:
    device = SetupDevice("gen9")
    rig = Rig(tmp_path, {"h9": device})
    assert rig.run("h9").hosts[0].status == "ready"
    posts, created = len(device.account_posts), list(rig.secrets.created)
    second = rig.run("h9")
    assert second.hosts[0].status == "ready"
    assert len(device.account_posts) == posts == 2
    assert rig.secrets.created == created
    states = {s.step: s.state for s in second.hosts[0].steps}
    assert states["read account"] == "reused"
    assert states["write account"] == "reused"
    assert len(rig.overlay()["hosts"]) == 1  # type: ignore[arg-type]


def test_an_account_the_ilo_already_has_with_a_stored_password_is_reused(
    tmp_path: Path,
) -> None:
    device = SetupDevice(
        "gen9", accounts={"onex-ilo-read": READ_PRIVS, "onex-ilo-write": WRITE_PRIVS}
    )
    rig = Rig(
        tmp_path,
        {"h9": device},
        secrets_values={
            "ILO_H9_READ_USERNAME": "onex-ilo-read",
            "ILO_H9_READ_PASSWORD": "stored-read-pw",
            "ILO_H9_WRITE_USERNAME": "onex-ilo-write",
            "ILO_H9_WRITE_PASSWORD": "stored-write-pw",
        },
    )
    result = rig.run("h9")
    assert result.hosts[0].status == "ready"
    assert device.account_posts == []
    assert rig.secrets.created == []


def test_a_stored_credential_with_no_account_creates_the_account_with_that_password(
    tmp_path: Path,
) -> None:
    device = SetupDevice("gen9")
    rig = Rig(
        tmp_path,
        {"h9": device},
        secrets_values={
            "ILO_H9_READ_USERNAME": "custom-reader",
            "ILO_H9_READ_PASSWORD": "stored-read-pw",
        },
    )
    assert rig.run("h9").hosts[0].status == "ready"
    posted = {str(p["UserName"]): p["Password"] for p in device.account_posts}
    assert posted["custom-reader"] == "stored-read-pw"
    assert "ILO_H9_READ_PASSWORD" not in rig.secrets.created


def test_an_account_with_no_stored_password_is_refused_not_duplicated_or_reset(
    tmp_path: Path,
) -> None:
    device = SetupDevice("gen9", accounts={"onex-ilo-read": READ_PRIVS})
    rig = Rig(tmp_path, {"h9": device})
    result = rig.run("h9")
    assert result.hosts[0].refusal is R.ACCOUNT_EXISTS_NO_CREDENTIAL
    assert device.account_posts == []
    assert rig.secrets.created == []


def test_an_existing_account_with_wider_privileges_is_refused(tmp_path: Path) -> None:
    device = SetupDevice(
        "gen9", accounts={"onex-ilo-read": READ_PRIVS | {"iLOConfigPriv"}}
    )
    rig = Rig(
        tmp_path,
        {"h9": device},
        secrets_values={
            "ILO_H9_READ_USERNAME": "onex-ilo-read",
            "ILO_H9_READ_PASSWORD": "stored-read-pw",
        },
    )
    assert rig.run("h9").hosts[0].refusal is R.ACCOUNT_TOO_PRIVILEGED


def test_an_account_missing_a_needed_privilege_is_refused(tmp_path: Path) -> None:
    device = SetupDevice("gen9", accounts={"onex-ilo-write": {"LoginPriv"}})
    rig = Rig(
        tmp_path,
        {"h9": device},
        secrets_values={
            "ILO_H9_READ_USERNAME": "r",
            "ILO_H9_READ_PASSWORD": "stored-read-pw",
            "ILO_H9_WRITE_USERNAME": "onex-ilo-write",
            "ILO_H9_WRITE_PASSWORD": "stored-write-pw",
        },
    )
    assert rig.run("h9").hosts[0].refusal is R.ACCOUNT_PRIVILEGE_SHORT


def test_only_half_of_an_account_in_the_store_is_refused(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        {"h9": SetupDevice("gen9")},
        secrets_values={"ILO_H9_READ_USERNAME": "x"},
    )
    assert rig.run("h9").hosts[0].refusal is R.STORE_INCONSISTENT


def test_the_administrator_credential_comes_from_the_store_when_it_is_there(
    tmp_path: Path,
) -> None:
    rig = Rig(
        tmp_path,
        {"h9": SetupDevice("gen9")},
        secrets_values={
            "ILO_H9_ADMIN_USERNAME": "Administrator",
            "ILO_H9_ADMIN_PASSWORD": "stored-admin-pw",
            "ILO_H1_ADMIN_PASSWORD": "another-hosts-credential",
        },
        password=None,
    )
    result = rig.run("h9")
    assert result.hosts[0].status == "ready"
    assert rig.password_prompts == 0
    assert "ILO_H1_ADMIN_PASSWORD" not in rig.secrets.read
    assert "stored-admin-pw" not in rig.everything_printed(result)


def test_no_stored_credential_and_no_terminal_refuses_before_any_connection(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, {"h9": SetupDevice("gen9")}, password=None)
    result = rig.run("h9")
    assert result.hosts[0].refusal is R.ADMIN_CREDENTIAL_MISSING
    assert rig.fetches == []


def test_a_wrong_administrator_password_stops_that_host_and_the_next_one_still_runs(
    tmp_path: Path,
) -> None:
    bad = SetupDevice("gen8")
    bad.accept_users = frozenset({"someone-else"})
    rig = Rig(tmp_path, {"h8": bad, "h9": SetupDevice("gen9")})
    result = rig.run()
    assert [h.status for h in result.hosts] == ["refused", "ready"]
    assert result.hosts[0].refusal is R.AUTH_FAILED
    assert bad.account_posts == []


def test_an_unreachable_secret_store_refuses_every_host(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"h9": SetupDevice("gen9")})
    rig.secrets.down = True
    assert rig.run("h9").hosts[0].refusal is R.SECRET_STORE_UNAVAILABLE


def test_a_product_that_names_no_generation_is_refused(tmp_path: Path) -> None:
    device = SetupDevice("gen9")
    device.resources["/redfish/v1/systems/1/"]["Model"] = "ProLiant DL360"
    rig = Rig(tmp_path, {"h9": device})
    result = rig.run("h9")
    assert result.hosts[0].refusal is R.GENERATION_UNRECOGNIZED
    assert device.account_posts == []


def test_an_ilo_without_an_account_service_is_refused_not_guessed(
    tmp_path: Path,
) -> None:
    device = SetupDevice("gen9")
    del device.resources["/redfish/v1/"]["AccountService"]
    rig = Rig(tmp_path, {"h9": device})
    assert rig.run("h9").hosts[0].refusal is R.ENDPOINT_MISSING


def test_the_address_comes_from_the_command_then_the_overlay_then_the_table_then_the_prompt(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, {"h9": SetupDevice("gen9")}, table={"h9": "192.0.2.50"})
    rig.run("h9", addresses={"h9": "192.0.2.77"})
    assert rig.fetches[-1] == ("192.0.2.77", 443)
    rig.run("h9")  # the overlay row now holds it and wins over the table
    assert rig.fetches[-1] == ("192.0.2.77", 443)
    fresh = Rig(tmp_path / "b", {"h9": SetupDevice("gen9")}, table={"h9": "192.0.2.50"})
    fresh.run("h9")
    assert fresh.fetches[-1] == ("192.0.2.50", 443)
    typed = Rig(
        tmp_path / "c",
        {"h9": SetupDevice("gen9")},
        table={},
        typed_address="192.0.2.60",
    )
    typed.run("h9")
    assert typed.fetches[-1] == ("192.0.2.60", 443)


def test_no_address_anywhere_refuses_and_says_how_to_supply_one(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"h9": SetupDevice("gen9")}, table={})
    result = rig.run("h9")
    assert result.hosts[0].refusal is R.ADDRESS_MISSING
    assert "--ilo-address h9=" in (result.hosts[0].detail or "")
    assert rig.fetches == []


def test_a_serial_that_differs_from_the_recorded_one_is_refused_on_a_rerun(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, {"h9": SetupDevice("gen9")})
    assert rig.run("h9").hosts[0].status == "ready"
    rig.devices["h9"].resources["/redfish/v1/systems/1/"]["SerialNumber"] = (
        "OTHERSERIAL"
    )
    # re-pin evidence is unchanged, so only the identity check can stop this
    result = rig.run("h9")
    assert result.hosts[0].refusal is R.IDENTITY_MISMATCH
    assert rig.overlay()["hosts"][0]["expected_serial"] == GEN9_SERIAL  # type: ignore[index]


def test_a_node_reader_that_cannot_see_the_accounts_is_reported(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"h9": SetupDevice("gen9")}, reader_ready=False)
    result = rig.run("h9")
    assert result.hosts[0].status == "ready"
    assert result.hosts[0].node_reader_ready is False
    assert any("NO" in line and "node reader" in line for line in rig.lines)


def test_columns_the_setup_does_not_own_survive_a_rerun(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"h9": SetupDevice("gen9")})
    rig.run("h9")
    path = rig.overlay_dir / "overlay.yaml"
    document = yaml.safe_load(path.read_text())
    document["hosts"][0]["media_url_prefixes"] = ["http://192.0.2.10/iso/"]
    path.write_text(yaml.safe_dump(document))
    rig.run("h9")
    assert rig.overlay()["hosts"][0]["media_url_prefixes"] == ["http://192.0.2.10/iso/"]  # type: ignore[index]


def test_generated_passwords_are_long_and_mixed() -> None:
    values = {generate_password().get_secret_value() for _ in range(20)}
    assert len(values) == 20
    for value in values:
        assert len(value) == 28
        assert any(c.isdigit() for c in value)
        assert any(c.isupper() for c in value)
        assert any(c.islower() for c in value)


def test_secret_values_never_reach_a_repr_or_the_result_json() -> None:
    password = generate_password()
    assert password.get_secret_value() not in repr(password)


def test_the_setup_verb_is_parsed_and_refuses_without_hosts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from omnimarket.nodes.node_ilo_redfish_effect.runtime_ilo_redfish_cli import main

    assert main(["--verb", "setup"]) == 2
    assert "request_invalid" in capsys.readouterr().out


def test_the_lab_host_table_row_accepts_an_ilo_address() -> None:
    from omnimarket.models.overlay_keys.model_overlay_documents import ModelLabHostRow

    row = ModelLabHostRow.model_validate(
        {"name": "h9", "target": "h9", "ilo_address": "192.0.2.52"}
    )
    assert row.ilo_address == "192.0.2.52"
    assert (
        ModelLabHostRow.model_validate({"name": "h9", "target": "h9"}).ilo_address
        is None
    )
