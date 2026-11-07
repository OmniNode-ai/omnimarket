# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The iLO setup verb: the five hand steps of the skill, in one run, for one or more hosts (OMN-22870).

Per host, in order: find the iLO address; read the administrator credential from the lab secret store
(key names are listed first, a value is read only for a name that is there) or take that host's hidden
prompt (with a "same as the previous host?" shortcut) and write it to the store at once, before any
contact with the iLO, so no later run asks for it again; fetch the certificate. An RSA key under 2048
bits is replaced first: the operator confirms its SHA-256 against the iLO's own certificate page (the
only bootstrap trust), the iLO generates a CSR, the lab CA (created once, its key only in the store)
signs it, the iLO imports it and resets, and the setup reconnects verifying the lab CA. Then, over a
connection pinned to exactly the certificate, log in and compare it with what the iLO reports about
its own certificate (a mismatch refuses before anything is created or written); read the generation
from the Redfish product; create the read account (Login only) and the write account (Login, Virtual
Media, Virtual Power and Reset) unless they exist, with generated passwords written to the store first
and create-only, so a rerun reuses and never duplicates; pin the certificate; write the overlay row;
set it live; run ``discover`` and ``identity`` and record the serial.

No credential is printed, logged, returned or put in a file or argument: passwords are ``SecretStr``
values that only the store port and the Redfish body see.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import secrets
import ssl
import string
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from pydantic import JsonValue, SecretStr, ValidationError

from omnimarket.nodes.node_ilo_redfish_effect.handlers import (
    setup_certificate as cert,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers import setup_lab_ca as lab_ca
from omnimarket.nodes.node_ilo_redfish_effect.handlers.handler_ilo_redfish import (
    HandlerIloRedfish,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.journal_file import (
    FileActionJournal,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.protocols import (
    ModelRedfishResponse,
    ProtocolRedfishTransport,
    RedfishTransportError,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.redfish_client import (
    IloRefusalError,
    JsonObject,
    RedfishSession,
    discover,
    link_of,
    message_id,
    odata_ids,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.setup_ports import (
    AskSamePassword,
    ConfirmCertificate,
    Emit,
    FetchCertificate,
    PasswordGenerator,
    PromptAddress,
    PromptAdminPassword,
    ProtocolSetupSecrets,
    SetupStoreError,
)
from omnimarket.models.ilo_redfish.enum_ilo_redfish import (
    EnumIloOutcome,
    EnumIloVerb,
)
from omnimarket.models.ilo_redfish.model_ilo_overlay import (
    ModelIloHost,
    ModelIloOverlay,
    credential_key,
)
from omnimarket.models.ilo_redfish.model_ilo_redfish_request import (
    ModelIloRedfishRequest,
)
from omnimarket.models.ilo_redfish.model_ilo_redfish_result import (
    ModelIloApproval,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_setup import (
    EnumIloSetupRefusal as Refusal,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_setup import (
    ModelIloSetupHostResult,
    ModelIloSetupRequest,
    ModelIloSetupResult,
    ModelIloSetupStep,
)

OVERLAY_NODE_DIR = "node_ilo_redfish_effect"
ILO_PORT = 443
READ_ACCOUNT_NAME = "onex-ilo-read"
WRITE_ACCOUNT_NAME = "onex-ilo-write"

_ALL_PRIVILEGES = (
    "LoginPriv",
    "RemoteConsolePriv",
    "UserConfigPriv",
    "VirtualMediaPriv",
    "VirtualPowerAndResetPriv",
    "iLOConfigPriv",
)
# Least privilege: the read account may log in and nothing else; the write account adds exactly the
# privileges the node's three write verbs need. Never Configure iLO Settings, Administer User
# Accounts or Remote Console.
_WANTED: dict[str, frozenset[str]] = {
    "READ": frozenset({"LoginPriv"}),
    "WRITE": frozenset({"LoginPriv", "VirtualMediaPriv", "VirtualPowerAndResetPriv"}),
}
_GENERATION = re.compile(r"\bgen\s?(8|9)\b", re.IGNORECASE)
_REFUSAL_FROM_NODE = {
    "tls_failed": Refusal.TLS_FAILED,
    "connect_failed": Refusal.CONNECT_FAILED,
    "auth_failed": Refusal.AUTH_FAILED,
    "insufficient_privilege": Refusal.INSUFFICIENT_PRIVILEGE,
    "endpoint_missing": Refusal.ENDPOINT_MISSING,
}


CSR_COUNTRY = "US"
CSR_STATE = "Lab"
CSR_CITY = "Lab"
CSR_ORGANIZATION = "OmniNode"
CSR_UNIT = "Lab infrastructure"
CSR_POLLS = 60
CSR_POLL_SECONDS = 10.0
RESET_POLLS = 60
RESET_POLL_SECONDS = 10.0


class _LegacyScope:
    """What the weak-key connection may carry: reads, the sign-in, and the two certificate actions.

    Everything else (a PATCH, a DELETE of anything but a session, a POST to any other path) never
    reaches the device.
    """

    def __init__(self, inner: ProtocolRedfishTransport) -> None:
        self._inner = inner
        self._posts: set[str] = set()

    @staticmethod
    def _norm(path: str) -> str:
        return path.lower().rstrip("/")

    def allow_post(self, path: str) -> None:
        self._posts.add(self._norm(path))

    def request(
        self,
        method: Literal["GET", "POST", "PATCH", "DELETE"],
        path: str,
        *,
        body: JsonValue = None,
        token: SecretStr | None = None,
    ) -> ModelRedfishResponse:
        target = self._norm(path)
        in_sessions = target.endswith("/sessions") or "/sessions/" in target + "/"
        allowed = (
            method == "GET"
            or (
                method == "POST"
                and (target in self._posts or target.endswith("/sessions"))
            )
            or (method == "DELETE" and in_sessions and not target.endswith("/sessions"))
        )
        if not allowed:
            raise RedfishTransportError("protocol", request_sent=False)
        return self._inner.request(method, path, body=body, token=token)


def _read_csr(pem: str) -> x509.CertificateSigningRequest:
    try:
        return lab_ca.read_csr(pem)
    except lab_ca.LabCaError as exc:
        raise _StopError(Refusal.CSR_FAILED, str(exc)) from None


class _StopError(Exception):
    """One host's setup cannot go on. Carries the refusal and a short, device-free detail."""

    def __init__(self, refusal: Refusal, detail: str | None = None) -> None:
        super().__init__(refusal.value)
        self.refusal = refusal
        self.detail = detail


class _NoApprovals:
    """The setup runs read verbs only, so no approval row is ever looked up."""

    def find(self, ref: str) -> ModelIloApproval | None:
        del ref
        return None


class _StoreCredentials:
    """The read account for the setup's own first read, straight from the store the accounts were put in."""

    def __init__(self, secrets_port: ProtocolSetupSecrets) -> None:
        self._secrets = secrets_port

    def get(self, key: str) -> SecretStr | None:
        try:
            return self._secrets.get(key)
        except SetupStoreError:
            return None


def generate_password() -> SecretStr:
    """28 characters from the system random source, with every character class present."""
    alphabet = string.ascii_letters + string.digits + "-_"
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(28))
        if (
            any(c.islower() for c in candidate)
            and any(c.isupper() for c in candidate)
            and any(c.isdigit() for c in candidate)
        ):
            return SecretStr(candidate)


def _find_link(body: JsonValue, name: str) -> str | None:
    """The first nested ``name`` link of a body, followed only as an API path."""
    if isinstance(body, dict):
        direct = link_of({name: body.get(name)}, name) if name in body else None
        if direct is not None:
            return direct
        for value in body.values():
            found = _find_link(value, name)
            if found is not None:
                return found
    elif isinstance(body, list):
        for item in body:
            found = _find_link(item, name)
            if found is not None:
                return found
    return None


def _granted(body: JsonObject) -> frozenset[str] | None:
    """The privileges an account body grants, or None when it states none."""
    stack: list[JsonValue] = [body]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            privileges = node.get("Privileges")
            if isinstance(privileges, dict):
                return frozenset(k for k, v in privileges.items() if v is True)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


class IloSetupSequence:
    """One request in, one redacted result out. Never raises for a refusal."""

    def __init__(
        self,
        *,
        secrets_port: ProtocolSetupSecrets,
        fetch_certificate: FetchCertificate,
        transport_factory: Callable[[ModelIloHost, Path], ProtocolRedfishTransport],
        legacy_transport_factory: Callable[
            [ModelIloHost, Path], ProtocolRedfishTransport
        ],
        ask_same_password: AskSamePassword,
        confirm_certificate: ConfirmCertificate,
        prompt_admin_password: PromptAdminPassword,
        prompt_address: PromptAddress,
        host_table_addresses: Mapping[str, str],
        overlay_dir: Path,
        journal_dir: Path,
        credential_key_template: str,
        node_reader: Callable[[str], object] | None,
        clock: Callable[[], datetime],
        sleeper: Callable[[float], None],
        emit: Emit,
        password_generator: PasswordGenerator = generate_password,
    ) -> None:
        self._secrets = secrets_port
        self._fetch = fetch_certificate
        self._transport_factory = transport_factory
        self._legacy_factory = legacy_transport_factory
        self._ask_same = ask_same_password
        self._confirm = confirm_certificate
        self._prompt_password = prompt_admin_password
        self._prompt_address = prompt_address
        self._table = host_table_addresses
        self._overlay_dir = overlay_dir
        self._journal_dir = journal_dir
        self._template = credential_key_template
        self._node_reader = node_reader
        self._clock = clock
        self._sleeper = sleeper
        self._emit = emit
        self._generate = password_generator
        # The previous host's administrator password of this run, for the "same as previous?" shortcut.
        self._last_admin: tuple[str, SecretStr] | None = None
        self._address_source = ""
        self._candidate_transport: ProtocolRedfishTransport | None = None

    # -- entry ------------------------------------------------------------------------------

    def handle(self, request: ModelIloSetupRequest) -> ModelIloSetupResult:
        results = []
        for name in dict.fromkeys(request.hosts):
            self._emit(f"== {name}")
            steps: list[ModelIloSetupStep] = []
            try:
                results.append(self._setup_host(name, request, steps))
            except _StopError as stop:
                steps.append(
                    ModelIloSetupStep(
                        step="stopped", state="refused", note=stop.refusal.value
                    )
                )
                self._emit(
                    f"   REFUSED {stop.refusal.value} {stop.detail or ''}".rstrip()
                )
                results.append(
                    ModelIloSetupHostResult(
                        host=name,
                        status="refused",
                        refusal=stop.refusal,
                        detail=stop.detail,
                        steps=tuple(steps),
                    )
                )
        return ModelIloSetupResult(hosts=tuple(results))

    # -- one host ---------------------------------------------------------------------------

    def _note(
        self,
        steps: list[ModelIloSetupStep],
        step: str,
        state: Literal["done", "reused", "refused"],
        note: str = "",
    ) -> None:
        steps.append(ModelIloSetupStep(step=step, state=state, note=note))
        self._emit(f"   {state:<7} {step}{(' - ' + note) if note else ''}")

    def _setup_host(
        self, name: str, request: ModelIloSetupRequest, steps: list[ModelIloSetupStep]
    ) -> ModelIloSetupHostResult:
        existing = self._existing_row(name)
        address = self._address(name, request, existing)
        self._note(steps, "address", "done", "from " + self._address_source)
        try:
            stored = self._secrets.names()
        except SetupStoreError as exc:
            raise _StopError(Refusal.SECRET_STORE_UNAVAILABLE, str(exc)) from None
        mine = sorted(k for k in stored if k.startswith(self._prefix(name)))
        self._note(
            steps,
            "store key names",
            "done",
            f"{len(mine)} for this host" if mine else "none for this host",
        )
        admin_user, admin_password = self._admin(name, request, stored, steps)

        port = _recorded_port(existing)
        der = self._fetch_cert(address, port or ILO_PORT)
        seen = cert.presented(der)
        issuer_pem: str | None = None
        if cert.is_weak(seen):
            try:
                der, issuer_pem = self._replace_weak_certificate(
                    name,
                    request,
                    address,
                    seen,
                    admin_user,
                    admin_password,
                    stored,
                    steps,
                    port,
                )
            except IloRefusalError as exc:
                raise _StopError(
                    _REFUSAL_FROM_NODE.get(exc.refusal.value, Refusal.READ_FAILED),
                    exc.detail,
                ) from None
            except RedfishTransportError as exc:
                raise _StopError(
                    Refusal.TLS_FAILED if exc.kind == "tls" else Refusal.CONNECT_FAILED,
                    exc.kind,
                ) from None
            seen = cert.presented(der)
        elif not seen.self_signed:
            issuer_pem = self._lab_issuer_pem(der, stored)
        else:
            self._trust_self_signed(name, address, seen, existing, steps)
        with tempfile.TemporaryDirectory(prefix="ilo-candidate-") as candidate_dir:
            pem = ssl.DER_cert_to_PEM_cert(der)
            Path(candidate_dir, f"{name}-ilo.pem").write_text(pem, encoding="ascii")
            if issuer_pem is not None:
                Path(candidate_dir, lab_ca.LAB_CA_FILE).write_text(
                    issuer_pem, encoding="ascii"
                )
            candidate = ModelIloHost.model_validate(
                _host_row(
                    name,
                    "gen9",
                    address,
                    "unverified",
                    seen.sha256,
                    f"{name}-ilo.pem",
                    False,
                    issuer_file=lab_ca.LAB_CA_FILE if issuer_pem else None,
                    port=port,
                )
            )
            self._candidate_transport = self._transport_factory(
                candidate, Path(candidate_dir)
            )
            session = RedfishSession(self._candidate_transport)
            try:
                return self._with_admin_session(
                    name,
                    request,
                    steps,
                    session,
                    address,
                    seen,
                    pem,
                    existing,
                    admin_user,
                    admin_password,
                    stored,
                    issuer_pem,
                    Path(candidate_dir),
                )
            except IloRefusalError as exc:
                raise _StopError(
                    _REFUSAL_FROM_NODE.get(exc.refusal.value, Refusal.READ_FAILED),
                    exc.detail,
                ) from None
            except RedfishTransportError as exc:
                raise _StopError(
                    Refusal.TLS_FAILED if exc.kind == "tls" else Refusal.CONNECT_FAILED,
                    exc.kind,
                ) from None
            finally:
                session.logout()

    def _with_admin_session(
        self,
        name: str,
        request: ModelIloSetupRequest,
        steps: list[ModelIloSetupStep],
        session: RedfishSession,
        address: str,
        seen: cert.PresentedCertificate,
        pem: str,
        existing: dict[str, JsonValue] | None,
        admin_user: str,
        admin_password: SecretStr,
        stored: frozenset[str],
        issuer_pem: str | None,
        candidate_dir: Path,
    ) -> ModelIloSetupHostResult:
        root = session.read_root()
        session.login(root, SecretStr(admin_user), admin_password)
        endpoints = discover(session, root)
        manager = session.get(endpoints.manager)
        self._check_certificate(session, manager, seen, steps)
        system = session.get(endpoints.system)
        generation, product = self._generation(system)
        self._note(steps, "generation", "done", f"{generation} from product {product}")

        collection = self._accounts_collection(session, root)
        for role in ("READ", "WRITE"):
            self._account(
                name, role, session, root, collection, stored, steps, existing
            )

        ca_file = f"{name}-ilo.pem"
        row = _host_row(
            name,
            generation,
            address,
            product,
            seen.sha256,
            ca_file,
            False,
            existing=existing,
            issuer_file=lab_ca.LAB_CA_FILE if issuer_pem else None,
            port=_recorded_port(existing),
        )
        # The candidate is checked from its own directory; the overlay row and the certificate files of a
        # host that already works are replaced only once the candidate has passed its first read.
        serial = self._first_read(name, row, existing, steps, candidate_dir)
        self._write_pem(ca_file, pem)
        if issuer_pem is not None:
            self._write_pem(lab_ca.LAB_CA_FILE, issuer_pem)
        self._note(
            steps,
            "certificate pin",
            "done",
            f"sha256 {seen.sha256[:16]}... saved as {ca_file}"
            + (" (signed by the lab CA, chain verified)" if issuer_pem else ""),
        )
        row = {**row, "live": True, "expected_serial": serial}
        self._write_overlay(row)
        self._note(
            steps,
            "overlay row",
            "done",
            "written live; first read passed and serial recorded",
        )
        self._note(steps, "live", "done", "live true; serial recorded")
        ready = self._reader_ready(name, row)
        self._emit(
            "   node reader sees the stored accounts: "
            + {True: "yes", False: "NO (see the note below)", None: "not checked"}[
                ready
            ]
        )
        return ModelIloSetupHostResult(
            host=name, status="ready", steps=tuple(steps), node_reader_ready=ready
        )

    # -- address, credentials ---------------------------------------------------------------

    def _prefix(self, host: str) -> str:
        return (
            credential_key(self._template, host=host, role="", field="").rstrip("_")
            + "_"
        )

    def _existing_row(self, name: str) -> dict[str, JsonValue] | None:
        document = self._overlay_document()
        for row in _rows(document):
            if row.get("name") == name:
                return row
        return None

    def _overlay_path(self) -> Path:
        return self._overlay_dir / "overlay.yaml"

    def _overlay_document(self) -> dict[str, JsonValue]:
        path = self._overlay_path()
        if not path.is_file():
            return {}
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            raise _StopError(
                Refusal.OVERLAY_INVALID, "existing overlay is unreadable"
            ) from None
        if not isinstance(raw, dict):
            raise _StopError(
                Refusal.OVERLAY_INVALID, "existing overlay is not a mapping"
            )
        return raw

    def _address(
        self,
        name: str,
        request: ModelIloSetupRequest,
        existing: dict[str, JsonValue] | None,
    ) -> str:
        explicit = request.addresses.get(name)
        if explicit:
            self._address_source = "the command"
            return explicit
        row_address = existing.get("address") if existing else None
        if isinstance(row_address, str) and row_address:
            self._address_source = "the existing overlay row"
            return row_address
        table = self._table.get(name)
        if table:
            self._address_source = "the lab host table"
            return table
        asked = self._prompt_address(name)
        if asked:
            self._address_source = "the prompt"
            return asked
        raise _StopError(
            Refusal.ADDRESS_MISSING,
            "the lab host table row has no ilo_address; pass --ilo-address "
            f"{name}=<address>",
        )

    def _key(self, host: str, role: str, field: str) -> str:
        return credential_key(self._template, host=host, role=role, field=field)

    def _account_keys(
        self, host: str, role: str, row: dict[str, JsonValue] | None
    ) -> tuple[str, str]:
        """The store keys of one account: the names the host's row retains, else the template's.

        This is the mapping the node's own reads resolve, so provisioning, the first read and the
        readiness check all use the same keys.
        """
        names = row.get("credentials") if row else None
        if not isinstance(names, dict):
            return self._key(host, role, "USERNAME"), self._key(host, role, "PASSWORD")
        user = names.get(f"{role.lower()}_username_key")
        second = names.get(f"{role.lower()}_password_key")
        if not isinstance(user, str) or not isinstance(second, str):
            raise _StopError(
                Refusal.STORE_INCONSISTENT,
                f"the overlay row's credentials do not name a {role.lower()} account",
            )
        return user, second

    def _admin(
        self,
        host: str,
        request: ModelIloSetupRequest,
        stored: frozenset[str],
        steps: list[ModelIloSetupStep],
    ) -> tuple[str, SecretStr]:
        """This host's administrator credential: the store first, else typed once and stored at once.

        A typed password is written to the store before any iLO call, so a run that stops later never
        makes the operator type it again; the next run finds it in the store and asks nothing.
        """
        user_entry = self._key(host, "ADMIN", "USERNAME")
        second_key = self._key(host, "ADMIN", "PASSWORD")
        if second_key in stored:
            try:
                stored_secret = self._secrets.get(second_key)
                user = self._secrets.get(user_entry) if user_entry in stored else None
            except SetupStoreError as exc:
                raise _StopError(Refusal.SECRET_STORE_UNAVAILABLE, str(exc)) from None
            if stored_secret is not None:
                name = user.get_secret_value() if user else request.admin_user
                self._note(
                    steps,
                    "administrator credential",
                    "reused",
                    f"from the store ({second_key})",
                )
                self._last_admin = (host, stored_secret)
                return name, stored_secret
        previous_host = self._previous_host_to_reuse(host)
        if previous_host is not None and self._last_admin is not None:
            how = f"same as {previous_host}"
            typed: SecretStr | None = self._last_admin[1]
        else:
            how = "from the hidden prompt"
            typed = self._prompt_password(host, request.admin_user)
        if typed is None:
            raise _StopError(
                Refusal.ADMIN_CREDENTIAL_MISSING,
                f"no {second_key} in the store and no terminal to ask on",
            )
        try:
            created = self._secrets.create(second_key, typed)
            if user_entry not in stored:
                self._secrets.create(user_entry, SecretStr(request.admin_user))
        except SetupStoreError as exc:
            raise _StopError(Refusal.SECRET_STORE_UNAVAILABLE, str(exc)) from None
        if not created:
            raise _StopError(
                Refusal.STORE_INCONSISTENT,
                f"{second_key} appeared in the store during this run",
            )
        self._note(
            steps,
            "administrator credential",
            "done",
            f"{how}; stored as {second_key}, so no later run asks again",
        )
        self._last_admin = (host, typed)
        return request.admin_user, typed

    def _previous_host_to_reuse(self, host: str) -> str | None:
        """The previous host of this run when the operator says its administrator password is the same."""
        previous = self._last_admin
        if (
            previous is not None
            and previous[0] != host
            and self._ask_same(host, previous[0])
        ):
            return previous[0]
        return None

    # -- certificate ------------------------------------------------------------------------

    def _fetch_cert(self, address: str, port: int) -> bytes:
        try:
            return self._fetch(address, port)
        except TimeoutError:
            raise _StopError(Refusal.CONNECT_FAILED, "timeout") from None
        except (ssl.SSLError, ValueError):
            raise _StopError(Refusal.TLS_FAILED, "handshake") from None
        except OSError:
            raise _StopError(Refusal.CONNECT_FAILED, "connect") from None

    def _trust_self_signed(
        self,
        name: str,
        address: str,
        seen: cert.PresentedCertificate,
        existing: dict[str, JsonValue] | None,
        steps: list[ModelIloSetupStep],
    ) -> None:
        """Trust a self-signed certificate before any credential is sent over it.

        The iLO's own report of its certificate is read over the connection that carries the
        administrator password, so it cannot authenticate that connection. The trust comes from the pin
        this host already has, or from the operator comparing the fingerprint with the iLO's own
        certificate page.
        """
        pinned = _recorded_pin(existing)
        if pinned is not None:
            if pinned != seen.sha256:
                raise _StopError(
                    Refusal.FINGERPRINT_MISMATCH,
                    "the certificate on the connection is not the one pinned for this host; "
                    "nothing was sent to the iLO",
                )
            self._note(
                steps,
                "certificate trust",
                "reused",
                "matches the pin recorded for this host",
            )
            return
        fingerprint = cert.colon_fingerprint(seen.sha256)
        for line in (
            f"   {name}'s iLO presents a self-signed certificate nothing has vouched for yet.",
            f"   SHA-256 {fingerprint}",
            f"   serial  {seen.serial}",
            "   Open the iLO's own certificate page (Administration, Security, SSL Certificate)",
            "   and compare. Only an exact match lets the setup send a password.",
        ):
            self._emit(line)
        if not self._confirm(
            name, address, fingerprint, seen.serial, seen.rsa_bits or 0
        ):
            raise _StopError(
                Refusal.FINGERPRINT_UNCONFIRMED,
                "the certificate fingerprint was not confirmed; nothing was sent to the iLO",
            )
        self._note(
            steps,
            "certificate trust",
            "done",
            "sha256 confirmed by the operator against the iLO's page",
        )

    def _https_cert(
        self, session: RedfishSession, manager: JsonObject
    ) -> tuple[str, JsonObject]:
        """The iLO's own HttpsCert resource: its path and body."""
        security = _find_link(manager, "SecurityService")
        security_body = session.get_optional(security)
        https_cert = _find_link(security_body or {}, "HttpsCert")
        body = session.get_optional(https_cert)
        if https_cert is None or body is None:
            raise _StopError(
                Refusal.FINGERPRINT_UNCONFIRMED, "the iLO offers no HttpsCert resource"
            )
        return https_cert, body

    def _check_certificate(
        self,
        session: RedfishSession,
        manager: JsonObject,
        seen: cert.PresentedCertificate,
        steps: list[ModelIloSetupStep],
    ) -> tuple[str, JsonObject]:
        path, body = self._https_cert(session, manager)
        told = cert.reported(body)
        for line in cert.side_by_side(seen, told):
            self._emit(line)
        if told.serial is None:
            raise _StopError(
                Refusal.FINGERPRINT_UNCONFIRMED,
                "the iLO reported no certificate serial",
            )
        if not cert.matches(seen, told):
            raise _StopError(
                Refusal.FINGERPRINT_MISMATCH,
                "the certificate on the connection is not the one the iLO reports; nothing was pinned",
            )
        basis = "serial and sha256" if told.sha256 else "serial"
        self._note(
            steps,
            "certificate check",
            "done",
            f"presented and reported agree on {basis}",
        )
        return path, body

    # -- weak certificate replacement -------------------------------------------------------

    def _lab_issuer_pem(self, der: bytes, stored: frozenset[str]) -> str:
        """The lab CA's PEM for a certificate that is not self-signed, or a refusal.

        Only a certificate the lab CA signed is trusted this way; any other issuer is refused.
        """
        ca = self._existing_lab_ca(stored)
        if ca is None or not lab_ca.issued_by(x509.load_der_x509_certificate(der), ca):
            raise _StopError(
                Refusal.ISSUER_UNKNOWN,
                "the certificate is not self-signed and the lab CA did not sign it; nothing was pinned",
            )
        return ca.certificate_pem

    def _existing_lab_ca(self, stored: frozenset[str]) -> lab_ca.LabCa | None:
        if lab_ca.LAB_CA_STORE_KEY not in stored:
            return None
        try:
            value = self._secrets.get(lab_ca.LAB_CA_STORE_KEY)
        except SetupStoreError as exc:
            raise _StopError(Refusal.SECRET_STORE_UNAVAILABLE, str(exc)) from None
        if value is None:
            raise _StopError(Refusal.LAB_CA_UNAVAILABLE, "the stored lab CA is empty")
        try:
            return lab_ca.load_lab_ca(value)
        except lab_ca.LabCaError as exc:
            raise _StopError(Refusal.LAB_CA_UNAVAILABLE, str(exc)) from None

    def _lab_ca(
        self, stored: frozenset[str], steps: list[ModelIloSetupStep]
    ) -> lab_ca.LabCa:
        """The lab CA from the store; created once, its key kept nowhere else."""
        existing = self._existing_lab_ca(stored)
        if existing is not None:
            self._note(
                steps, "lab CA", "reused", f"from the store ({lab_ca.LAB_CA_STORE_KEY})"
            )
            return existing
        fresh = lab_ca.create_lab_ca(self._clock())
        try:
            created = self._secrets.create(lab_ca.LAB_CA_STORE_KEY, fresh.bundle())
        except SetupStoreError as exc:
            raise _StopError(Refusal.SECRET_STORE_UNAVAILABLE, str(exc)) from None
        if not created:
            # another run created it first: use that one and never ours
            raced = self._existing_lab_ca(frozenset({lab_ca.LAB_CA_STORE_KEY}))
            if raced is None:
                raise _StopError(Refusal.LAB_CA_UNAVAILABLE, "lab CA vanished")
            return raced
        self._note(
            steps,
            "lab CA",
            "done",
            f"created; its key is kept only in the store ({lab_ca.LAB_CA_STORE_KEY})",
        )
        return fresh

    def _replace_weak_certificate(
        self,
        name: str,
        request: ModelIloSetupRequest,
        address: str,
        seen: cert.PresentedCertificate,
        admin_user: str,
        admin_password: SecretStr,
        stored: frozenset[str],
        steps: list[ModelIloSetupStep],
        port: int | None,
    ) -> tuple[bytes, str]:
        """Replace an RSA key too weak for a modern client: the new certificate's DER and the CA's PEM.

        The only trust on the weak connection is the SHA-256 the operator confirmed against the iLO's
        own certificate page, enforced by the legacy transport before any request byte; that
        connection serves only the sign-in, the HttpsCert reads and the two certificate actions.
        """
        del request
        fingerprint = cert.colon_fingerprint(seen.sha256)
        for line in (
            f"   {name}'s iLO presents an RSA {seen.rsa_bits}-bit certificate, too weak for a modern client.",
            f"   SHA-256 {fingerprint}",
            f"   serial  {seen.serial}",
            "   Open the iLO's own certificate page (Administration, Security, SSL Certificate)",
            "   and compare. Only an exact match lets the setup replace it.",
        ):
            self._emit(line)
        if not self._confirm(
            name, address, fingerprint, seen.serial, seen.rsa_bits or 0
        ):
            raise _StopError(
                Refusal.FINGERPRINT_UNCONFIRMED,
                "the certificate fingerprint was not confirmed; nothing was sent to the iLO",
            )
        self._note(
            steps,
            "weak certificate",
            "done",
            f"RSA {seen.rsa_bits}-bit; sha256 confirmed by the operator against the iLO's page",
        )
        ca = self._lab_ca(stored, steps)
        with tempfile.TemporaryDirectory(prefix="ilo-legacy-") as work:
            legacy_host = ModelIloHost.model_validate(
                _host_row(
                    name,
                    "gen9",
                    address,
                    "unverified",
                    seen.sha256,
                    "legacy.pem",
                    False,
                    port=port,
                )
            )
            scope = _LegacyScope(self._legacy_factory(legacy_host, Path(work)))
            session = RedfishSession(scope)
            try:
                root = session.read_root()
                session.login(root, SecretStr(admin_user), admin_password)
                endpoints = discover(session, root)
                manager = session.get(endpoints.manager)
                path, body = self._check_certificate(session, manager, seen, steps)
                csr_pem = self._generate_csr(
                    session, scope, name, address, path, body, steps
                )
                leaf = lab_ca.sign_csr(
                    ca, _read_csr(csr_pem), address=address, now=self._clock()
                )
                leaf_pem = leaf.public_bytes(serialization.Encoding.PEM).decode("ascii")
                self._import_certificate(session, scope, path, body, leaf_pem, steps)
            finally:
                session.logout()
            leaf_der = leaf.public_bytes(serialization.Encoding.DER)
            self._await_verified(
                name, address, leaf_der, leaf_pem, ca, Path(work), steps, port
            )
        return leaf_der, ca.certificate_pem

    def _generate_csr(
        self,
        session: RedfishSession,
        scope: _LegacyScope,
        name: str,
        address: str,
        resource: str,
        body: JsonObject,
        steps: list[ModelIloSetupStep],
    ) -> str:
        action = cert.find_action(body, resource, "GenerateCSR")
        if action is None:
            raise _StopError(
                Refusal.CSR_UNSUPPORTED,
                "the iLO's HttpsCert resource lists no GenerateCSR action; actions it lists: "
                + (", ".join(cert.action_names(body)) or "none"),
            )
        scope.allow_post(action.target)
        parameters: JsonObject = {
            "Country": CSR_COUNTRY,
            "State": CSR_STATE,
            "City": CSR_CITY,
            "OrgName": CSR_ORGANIZATION,
            "OrgUnit": CSR_UNIT,
            "CommonName": f"{name}-ilo",
        }
        response = session.send_write(
            "POST", action.target, action.body("GenerateCSR", parameters)
        )
        self._expect_certificate_call(response, Refusal.CSR_FAILED, "GenerateCSR")
        pem = cert.signing_request_of(response.body)
        for _ in range(CSR_POLLS):
            if pem is not None:
                break
            self._sleeper(CSR_POLL_SECONDS)
            pem = cert.signing_request_of(session.get(resource))
        if pem is None:
            raise _StopError(
                Refusal.CSR_FAILED, "the iLO produced no signing request in time"
            )
        self._note(
            steps, "signing request", "done", f"generated by the iLO for {address}"
        )
        return pem

    def _import_certificate(
        self,
        session: RedfishSession,
        scope: _LegacyScope,
        resource: str,
        body: JsonObject,
        leaf_pem: str,
        steps: list[ModelIloSetupStep],
    ) -> None:
        action = cert.find_action(body, resource, "ImportCertificate")
        if action is None:
            raise _StopError(
                Refusal.CSR_UNSUPPORTED,
                "the iLO's HttpsCert resource lists no ImportCertificate action; actions it lists: "
                + (", ".join(cert.action_names(body)) or "none"),
            )
        scope.allow_post(action.target)
        try:
            response = session.send_write(
                "POST",
                action.target,
                action.body("ImportCertificate", {"Certificate": leaf_pem}),
            )
        except RedfishTransportError as exc:
            if not exc.request_sent:
                raise
            # The iLO may drop the connection as it resets; the verified reconnect decides.
            self._note(
                steps,
                "certificate import",
                "done",
                "sent; the iLO closed the connection to reset",
            )
            return
        self._expect_certificate_call(
            response, Refusal.CERTIFICATE_IMPORT_FAILED, "ImportCertificate"
        )
        self._note(
            steps, "certificate import", "done", "signed by the lab CA; the iLO resets"
        )

    @staticmethod
    def _expect_certificate_call(
        response: ModelRedfishResponse, refusal: Refusal, what: str
    ) -> None:
        if response.status in (200, 201, 202):
            return
        if response.status == 401:
            raise _StopError(Refusal.AUTH_FAILED, what)
        if response.status == 403:
            raise _StopError(
                Refusal.INSUFFICIENT_PRIVILEGE,
                f"{what}: {message_id(response.body) or 'refused'}",
            )
        raise _StopError(
            refusal,
            f"{what}: http {response.status} {message_id(response.body) or ''}".strip(),
        )

    def _await_verified(
        self,
        name: str,
        address: str,
        leaf_der: bytes,
        leaf_pem: str,
        ca: lab_ca.LabCa,
        work: Path,
        steps: list[ModelIloSetupStep],
        port: int | None,
    ) -> None:
        """Wait out the iLO's reset: connect normally, verifying the lab CA and the new fingerprint."""
        Path(work, "leaf.pem").write_text(leaf_pem, encoding="ascii")
        Path(work, lab_ca.LAB_CA_FILE).write_text(ca.certificate_pem, encoding="ascii")
        verified_host = ModelIloHost.model_validate(
            _host_row(
                name,
                "gen9",
                address,
                "unverified",
                hashlib.sha256(leaf_der).hexdigest(),
                "leaf.pem",
                False,
                issuer_file=lab_ca.LAB_CA_FILE,
                port=port,
            )
        )
        transport = self._transport_factory(verified_host, work)
        for attempt in range(RESET_POLLS):
            try:
                RedfishSession(transport).read_root()
            except IloRefusalError:
                self._sleeper(RESET_POLL_SECONDS)
                continue
            self._note(
                steps,
                "certificate replaced",
                "done",
                f"verified against the lab CA after {attempt + 1} connection attempt(s); "
                f"new sha256 {hashlib.sha256(leaf_der).hexdigest()[:16]}...",
            )
            return
        raise _StopError(
            Refusal.ILO_RESET_TIMEOUT,
            "the iLO did not come back with the new certificate in time; run the setup again",
        )

    # -- generation -------------------------------------------------------------------------

    @staticmethod
    def _generation(system: JsonObject) -> tuple[str, str]:
        model = str(system.get("Model") or "").strip()
        found = _GENERATION.search(model)
        if found is None:
            raise _StopError(
                Refusal.GENERATION_UNRECOGNIZED,
                "the Redfish product names no Gen8 or Gen9",
            )
        return f"gen{found.group(1)}", model

    # -- accounts ---------------------------------------------------------------------------

    @staticmethod
    def _accounts_collection(session: RedfishSession, root: JsonObject) -> str:
        service = link_of(root, "AccountService")
        body = session.get_optional(service)
        collection = link_of(body, "Accounts") if body is not None else None
        if collection is None:
            raise _StopError(Refusal.ENDPOINT_MISSING, "account service")
        return collection

    def _existing_accounts(
        self, session: RedfishSession, collection: str
    ) -> dict[str, JsonObject]:
        found: dict[str, JsonObject] = {}
        for member in odata_ids(session.get(collection)):
            body = session.get(member)
            user = body.get("UserName")
            if isinstance(user, str):
                found[user] = body
        return found

    def _account(
        self,
        host: str,
        role: str,
        session: RedfishSession,
        root: JsonObject,
        collection: str,
        stored: frozenset[str],
        steps: list[ModelIloSetupStep],
        row: dict[str, JsonValue] | None,
    ) -> None:
        label = f"{role.lower()} account"
        user_entry, second_key = self._account_keys(host, role, row)
        has_user, has_pass = user_entry in stored, second_key in stored
        if has_pass and not has_user:
            raise _StopError(
                Refusal.STORE_INCONSISTENT,
                f"the store holds {second_key} but not {user_entry}",
            )
        # A username alone is what a run that stopped between its two create-only writes leaves: no
        # password exists and no account has been created for it. It is resumed below unless the iLO
        # already has that account, which is refused because its password is not ours to replace.
        try:
            stored_user = self._secrets.get(user_entry) if has_user else None
            password = self._secrets.get(second_key) if has_pass else None
        except SetupStoreError:
            raise _StopError(
                Refusal.SECRET_STORE_UNAVAILABLE, "read of an account key"
            ) from None
        if has_user and stored_user is None:
            raise _StopError(Refusal.SECRET_STORE_UNAVAILABLE, "read of an account key")
        username = (
            stored_user.get_secret_value()
            if stored_user is not None
            else (READ_ACCOUNT_NAME if role == "READ" else WRITE_ACCOUNT_NAME)
        )
        accounts = self._existing_accounts(session, collection)
        wanted = _WANTED[role]
        present = accounts.get(username)
        if present is not None:
            if password is None:
                raise _StopError(
                    Refusal.ACCOUNT_EXISTS_NO_CREDENTIAL,
                    f"{label} exists in the iLO and {second_key} is not in the store; "
                    "remove the account in the iLO or store its password, then run again",
                )
            granted = _granted(present)
            if granted is None:
                raise _StopError(Refusal.ACCOUNT_PRIVILEGES_UNREADABLE, label)
            if granted - wanted:
                raise _StopError(
                    Refusal.ACCOUNT_TOO_PRIVILEGED,
                    f"{label} holds {sorted(granted - wanted)}; never widened or narrowed here",
                )
            if wanted - granted:
                raise _StopError(
                    Refusal.ACCOUNT_PRIVILEGE_SHORT,
                    f"{label} lacks {sorted(wanted - granted)}",
                )
            self._verify_login(root, username, password, label)
            self._note(
                steps,
                label,
                "reused",
                f"{username} already exists with exactly the wanted privileges",
            )
            return
        if password is None:
            password = self._store_new_account(
                role, username, user_entry, second_key, has_user, steps
            )
        self._create_account(session, collection, username, password, wanted, label)
        self._verify_login(root, username, password, label)
        self._note(steps, label, "done", f"{username} created with {sorted(wanted)}")

    def _store_new_account(
        self,
        role: str,
        username: str,
        user_entry: str,
        second_key: str,
        has_user: bool,
        steps: list[ModelIloSetupStep],
    ) -> SecretStr:
        password = self._generate()
        try:
            created_user = has_user or self._secrets.create(
                user_entry, SecretStr(username)
            )
            created_pass = self._secrets.create(second_key, password)
        except SetupStoreError as exc:
            raise _StopError(Refusal.SECRET_STORE_UNAVAILABLE, str(exc)) from None
        if not (created_user and created_pass):
            raise _StopError(
                Refusal.STORE_INCONSISTENT,
                f"{user_entry} or {second_key} appeared in the store during this run",
            )
        self._note(
            steps,
            f"{role.lower()} store keys",
            "done",
            f"{second_key} created; {user_entry} was already stored"
            if has_user
            else f"{user_entry}, {second_key} created",
        )
        return password

    @staticmethod
    def _create_account(
        session: RedfishSession,
        collection: str,
        username: str,
        password: SecretStr,
        wanted: frozenset[str],
        label: str,
    ) -> None:
        body: JsonValue = {
            "UserName": username,
            "Password": password.get_secret_value(),
            "Oem": {
                "Hp": {
                    "LoginName": username,
                    "Privileges": {p: p in wanted for p in _ALL_PRIVILEGES},
                }
            },
        }
        response: ModelRedfishResponse = session.send_write("POST", collection, body)
        if response.status in (200, 201):
            return
        if response.status == 401:
            raise _StopError(Refusal.AUTH_FAILED, label)
        if response.status == 403:
            raise _StopError(
                Refusal.INSUFFICIENT_PRIVILEGE,
                f"the administrator may not create accounts {message_id(response.body) or ''}".strip(),
            )
        raise _StopError(
            Refusal.ACCOUNT_CREATE_FAILED,
            f"{label}: http {response.status} {message_id(response.body) or ''}".strip(),
        )

    def _verify_login(
        self,
        root: JsonObject,
        username: str,
        password: SecretStr,
        label: str,
    ) -> None:
        """A second session as the account proves the stored password is the account's password."""
        if self._candidate_transport is None:
            raise _StopError(Refusal.ACCOUNT_LOGIN_FAILED, label)
        probe = RedfishSession(self._candidate_transport)
        try:
            probe.login(root, SecretStr(username), password)
        except IloRefusalError:
            raise _StopError(Refusal.ACCOUNT_LOGIN_FAILED, label) from None
        finally:
            probe.logout()

    # -- files ------------------------------------------------------------------------------

    def _write_pem(self, name: str, pem: str) -> None:
        self._write_private(self._overlay_dir / name, pem)

    def _write_private(self, path: Path, text: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # a name no other writer shares, created 0600, so that only this writer's finished text
            # is ever renamed into place
            fd, staged = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
            tmp = Path(staged)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(text)
                os.replace(tmp, path)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        except OSError:
            raise _StopError(Refusal.OVERLAY_WRITE_FAILED, path.name) from None

    @contextmanager
    def _overlay_lock(self) -> Iterator[None]:
        """Exclusive lock on the overlay for a whole read, merge and publish, across processes."""
        try:
            self._overlay_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                self._overlay_dir / "overlay.yaml.lock",
                os.O_RDWR | os.O_CREAT,
                0o600,
            )
        except OSError:
            raise _StopError(Refusal.OVERLAY_WRITE_FAILED, "overlay lock") from None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                raise _StopError(Refusal.OVERLAY_WRITE_FAILED, "overlay lock") from None
            yield
        finally:
            os.close(fd)

    def _prepare_journal_dir(self) -> None:
        try:
            self._journal_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self._journal_dir, 0o700)
        except OSError:
            raise _StopError(
                Refusal.OVERLAY_WRITE_FAILED, "journal directory"
            ) from None

    def _write_overlay(self, row: dict[str, JsonValue]) -> None:
        with self._overlay_lock():
            self._publish_overlay_row(row)

    def _publish_overlay_row(self, row: dict[str, JsonValue]) -> None:
        document = self._overlay_document()
        rows = _rows(document)
        merged: list[JsonValue] = [
            row if r.get("name") == row["name"] else r for r in rows
        ]
        if all(r.get("name") != row["name"] for r in rows):
            merged.append(row)
        document = {
            **document,
            "hosts": merged,
            "journal_dir": document.get("journal_dir") or str(self._journal_dir),
        }
        try:
            ModelIloOverlay.model_validate(document)
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
            raise _StopError(Refusal.OVERLAY_INVALID, ",".join(fields)) from None
        self._prepare_journal_dir()
        self._write_private(
            self._overlay_path(), yaml.safe_dump(document, sort_keys=False)
        )

    # -- first read -------------------------------------------------------------------------

    def _first_read(
        self,
        name: str,
        row: dict[str, JsonValue],
        existing: dict[str, JsonValue] | None,
        steps: list[ModelIloSetupStep],
        directory: Path,
    ) -> str:
        document = self._overlay_document()
        live = {**row, "live": True}
        live.pop("expected_serial", None)
        overlay = ModelIloOverlay.model_validate(
            {
                **document,
                "hosts": [live],
                "journal_dir": document.get("journal_dir") or str(self._journal_dir),
            }
        )
        self._prepare_journal_dir()
        handler = HandlerIloRedfish(
            overlay=overlay,
            transport_factory=lambda host: self._transport_factory(host, directory),
            credentials=_StoreCredentials(self._secrets),
            approvals=_NoApprovals(),
            journal=FileActionJournal(Path(overlay.journal_dir)),
            clock=self._clock,
            sleeper=self._sleeper,
            credential_key_template=self._template,
        )
        results = {}
        for verb in (EnumIloVerb.DISCOVER, EnumIloVerb.IDENTITY):
            result = handler.handle(
                ModelIloRedfishRequest(verb=verb, host=name, requested_by="ilo-setup")
            )
            if result.outcome is not EnumIloOutcome.OK:
                raise _StopError(
                    _REFUSAL_FROM_NODE.get(
                        result.refusal.value if result.refusal else "",
                        Refusal.READ_FAILED,
                    ),
                    f"{verb.value}: {result.refusal.value if result.refusal else result.outcome.value}",
                )
            results[verb] = result
        self._note(steps, "discover", "done", "ok")
        data = results[EnumIloVerb.IDENTITY].data or {}
        serial = data.get("serial")
        if (
            not isinstance(serial, str)
            or not serial
            or data.get("product_matches_overlay") is not True
            or data.get("generation_consistent") is not True
        ):
            raise _StopError(
                Refusal.IDENTITY_MISMATCH, "product, generation or serial unreadable"
            )
        recorded = existing.get("expected_serial") if existing else None
        if isinstance(recorded, str) and recorded and recorded != serial:
            raise _StopError(
                Refusal.IDENTITY_MISMATCH,
                "the live serial differs from the one already recorded for this host; "
                "this may not be the server the operator means",
            )
        self._note(steps, "identity", "done", f"serial {serial}")
        return serial

    def _reader_ready(self, name: str, row: dict[str, JsonValue]) -> bool | None:
        if self._node_reader is None:
            return None
        try:
            return all(
                self._node_reader(key) is not None
                for role in ("READ", "WRITE")
                for key in self._account_keys(name, role, row)
            )
        except Exception:  # any reader failure means the node cannot read them yet
            return False


def _rows(document: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    hosts = document.get("hosts")
    if not isinstance(hosts, list):
        return []
    return [r for r in hosts if isinstance(r, dict)]


def _recorded_port(existing: dict[str, JsonValue] | None) -> int | None:
    """The port a host's overlay row configures, or None when the row sets none."""
    port = existing.get("port") if existing else None
    return port if isinstance(port, int) and not isinstance(port, bool) else None


def _recorded_pin(existing: dict[str, JsonValue] | None) -> str | None:
    """The SHA-256 a host's overlay row already pins, if it pins one."""
    tls = existing.get("tls") if existing else None
    pin = tls.get("sha256") if isinstance(tls, dict) else None
    return pin.lower() if isinstance(pin, str) else None


def _host_row(
    name: str,
    generation: str,
    address: str,
    product: str,
    sha256: str,
    ca_file: str,
    live: bool,
    *,
    existing: dict[str, JsonValue] | None = None,
    issuer_file: str | None = None,
    port: int | None = None,
) -> dict[str, JsonValue]:
    """The overlay row; a column the setup does not own (port, media prefixes, key names) is kept."""
    kept = dict(existing or {})
    tls: dict[str, JsonValue] = {"ca_file": ca_file, "sha256": sha256}
    if issuer_file is not None:
        tls["issuer_file"] = issuer_file
    if port is not None:
        kept["port"] = port
    return {
        **kept,
        "name": name,
        "generation": generation,
        "address": address,
        "expected_product": product,
        "tls": tls,
        "live": live,
    }


__all__ = ["IloSetupSequence", "generate_password"]
