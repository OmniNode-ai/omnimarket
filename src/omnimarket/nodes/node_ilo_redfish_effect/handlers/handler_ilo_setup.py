# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The iLO setup verb: the five hand steps of the skill, in one run, for one or more hosts (OMN-22870).

Per host, in order: find the iLO address; read the administrator credential from the lab secret store
(key names are listed first, a value is read only for a name that is there) or take the one hidden
prompt of the run; fetch the certificate and, over a connection pinned to exactly that certificate, log
in and compare it with what the iLO reports about its own certificate (a mismatch refuses before
anything is created or written); read the generation from the Redfish product; create the read account
(Login only) and the write account (Login, Virtual Media, Virtual Power and Reset) unless they exist,
with generated passwords written to the store first and create-only, so a rerun reuses and never
duplicates; pin the certificate; write the overlay row; set it live; run ``discover`` and ``identity``
and record the serial.

No credential is printed, logged, returned or put in a file or argument: passwords are ``SecretStr``
values that only the store port and the Redfish body see.
"""

from __future__ import annotations

import os
import re
import secrets
import ssl
import string
import tempfile
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import JsonValue, SecretStr, ValidationError

from omnimarket.nodes.node_ilo_redfish_effect.handlers import setup_certificate as cert
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
    Emit,
    FetchCertificate,
    PasswordGenerator,
    PromptAddress,
    PromptAdminPassword,
    ProtocolSetupSecrets,
    SetupStoreError,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.enum_ilo_redfish import (
    EnumIloOutcome,
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


class HandlerIloSetup:
    """One request in, one redacted result out. Never raises for a refusal."""

    def __init__(
        self,
        *,
        secrets_port: ProtocolSetupSecrets,
        fetch_certificate: FetchCertificate,
        transport_factory: Callable[[ModelIloHost, Path], ProtocolRedfishTransport],
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
        self._prompted: SecretStr | None = None
        self._asked = False
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
        state: str,
        note: str = "",
    ) -> None:
        steps.append(ModelIloSetupStep(step=step, state=state, note=note))  # type: ignore[arg-type]
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
            steps, "store key names", "done", ", ".join(mine) or "none for this host"
        )
        admin_user, admin_password = self._admin(name, request, stored, steps)

        der = self._fetch_cert(address)
        seen = cert.presented(der)
        with tempfile.TemporaryDirectory(prefix="ilo-candidate-") as candidate_dir:
            pem = ssl.DER_cert_to_PEM_cert(der)
            Path(candidate_dir, "candidate.pem").write_text(pem, encoding="ascii")
            candidate = ModelIloHost.model_validate(
                _host_row(
                    name,
                    "gen9",
                    address,
                    "unverified",
                    seen.sha256,
                    "candidate.pem",
                    False,
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
            self._account(name, role, session, root, collection, stored, steps)

        ca_file = f"{name}-ilo.pem"
        self._write_pem(ca_file, pem)
        self._note(
            steps,
            "certificate pin",
            "done",
            f"sha256 {seen.sha256[:16]}... saved as {ca_file}",
        )
        row = _host_row(
            name,
            generation,
            address,
            product,
            seen.sha256,
            ca_file,
            False,
            existing=existing,
        )
        self._write_overlay(row)
        self._note(
            steps,
            "overlay row",
            "done",
            "written, live false until the first read passes",
        )

        serial = self._first_read(name, row, existing, steps)
        row = {**row, "live": True, "expected_serial": serial}
        self._write_overlay(row)
        self._note(steps, "live", "done", "live true; serial recorded")
        ready = self._reader_ready(name)
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

    def _admin(
        self,
        host: str,
        request: ModelIloSetupRequest,
        stored: frozenset[str],
        steps: list[ModelIloSetupStep],
    ) -> tuple[str, SecretStr]:
        user_key = self._key(host, "ADMIN", "USERNAME")
        pass_key = self._key(host, "ADMIN", "PASSWORD")
        if pass_key in stored:
            try:
                password = self._secrets.get(pass_key)
                user = self._secrets.get(user_key) if user_key in stored else None
            except SetupStoreError as exc:
                raise _StopError(Refusal.SECRET_STORE_UNAVAILABLE, str(exc)) from None
            if password is not None:
                self._note(
                    steps,
                    "administrator credential",
                    "reused",
                    f"from the store ({pass_key})",
                )
                return (
                    user.get_secret_value() if user else request.admin_user
                ), password
        if not self._asked:
            self._asked = True
            self._prompted = self._prompt_password(request.admin_user)
        if self._prompted is None:
            raise _StopError(
                Refusal.ADMIN_CREDENTIAL_MISSING,
                f"no {pass_key} in the store and no terminal to ask on",
            )
        self._note(steps, "administrator credential", "done", "from the hidden prompt")
        return request.admin_user, self._prompted

    # -- certificate ------------------------------------------------------------------------

    def _fetch_cert(self, address: str) -> bytes:
        try:
            return self._fetch(address, ILO_PORT)
        except TimeoutError:
            raise _StopError(Refusal.CONNECT_FAILED, "timeout") from None
        except (ssl.SSLError, ValueError):
            raise _StopError(Refusal.TLS_FAILED, "handshake") from None
        except OSError:
            raise _StopError(Refusal.CONNECT_FAILED, "connect") from None

    def _check_certificate(
        self,
        session: RedfishSession,
        manager: JsonObject,
        seen: cert.PresentedCertificate,
        steps: list[ModelIloSetupStep],
    ) -> None:
        security = _find_link(manager, "SecurityService")
        security_body = session.get_optional(security)
        https_cert = _find_link(security_body or {}, "HttpsCert")
        body = session.get_optional(https_cert)
        if body is None:
            raise _StopError(
                Refusal.FINGERPRINT_UNCONFIRMED, "the iLO offers no HttpsCert resource"
            )
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
    ) -> None:
        label = f"{role.lower()} account"
        user_key = self._key(host, role, "USERNAME")
        pass_key = self._key(host, role, "PASSWORD")
        has_user, has_pass = user_key in stored, pass_key in stored
        if has_user != has_pass:
            raise _StopError(
                Refusal.STORE_INCONSISTENT,
                f"the store holds only one of {user_key} and {pass_key}",
            )
        try:
            username = (
                self._secrets.get(user_key).get_secret_value()  # type: ignore[union-attr]
                if has_user
                else (READ_ACCOUNT_NAME if role == "READ" else WRITE_ACCOUNT_NAME)
            )
            password = self._secrets.get(pass_key) if has_pass else None
        except (SetupStoreError, AttributeError):
            raise _StopError(
                Refusal.SECRET_STORE_UNAVAILABLE, "read of an account key"
            ) from None
        accounts = self._existing_accounts(session, collection)
        wanted = _WANTED[role]
        present = accounts.get(username)
        if present is not None:
            if password is None:
                raise _StopError(
                    Refusal.ACCOUNT_EXISTS_NO_CREDENTIAL,
                    f"{label} exists in the iLO and {pass_key} is not in the store; "
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
            password = self._store_new_account(host, role, username, steps)
        self._create_account(session, collection, username, password, wanted, label)
        self._verify_login(root, username, password, label)
        self._note(steps, label, "done", f"{username} created with {sorted(wanted)}")

    def _store_new_account(
        self, host: str, role: str, username: str, steps: list[ModelIloSetupStep]
    ) -> SecretStr:
        user_key = self._key(host, role, "USERNAME")
        pass_key = self._key(host, role, "PASSWORD")
        password = self._generate()
        try:
            created_user = self._secrets.create(user_key, SecretStr(username))
            created_pass = self._secrets.create(pass_key, password)
        except SetupStoreError as exc:
            raise _StopError(Refusal.SECRET_STORE_UNAVAILABLE, str(exc)) from None
        if not (created_user and created_pass):
            raise _StopError(
                Refusal.STORE_INCONSISTENT,
                f"{user_key} or {pass_key} appeared in the store during this run",
            )
        self._note(
            steps,
            f"{role.lower()} store keys",
            "done",
            f"{user_key}, {pass_key} created",
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
            tmp = path.with_name(path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(tmp, path)
        except OSError:
            raise _StopError(Refusal.OVERLAY_WRITE_FAILED, path.name) from None

    def _write_overlay(self, row: dict[str, JsonValue]) -> None:
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
        self._journal_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self._journal_dir, 0o700)
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
    ) -> str:
        document = self._overlay_document()
        live = {**row, "live": True}
        live.pop("expected_serial", None)
        overlay = ModelIloOverlay.model_validate({**document, "hosts": [live]})
        overlay_dir = self._overlay_dir
        handler = HandlerIloRedfish(
            overlay=overlay,
            transport_factory=lambda host: self._transport_factory(host, overlay_dir),
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

    def _reader_ready(self, name: str) -> bool | None:
        if self._node_reader is None:
            return None
        try:
            return all(
                self._node_reader(self._key(name, role, field)) is not None
                for role in ("READ", "WRITE")
                for field in ("USERNAME", "PASSWORD")
            )
        except Exception:  # any reader failure means the node cannot read them yet
            return False


def _rows(document: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    hosts = document.get("hosts")
    if not isinstance(hosts, list):
        return []
    return [r for r in hosts if isinstance(r, dict)]


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
) -> dict[str, JsonValue]:
    """The overlay row; a column the setup does not own (port, media prefixes, key names) is kept."""
    kept = dict(existing or {})
    return {
        **kept,
        "name": name,
        "generation": generation,
        "address": address,
        "expected_product": product,
        "tls": {"ca_file": ca_file, "sha256": sha256},
        "live": live,
    }


__all__ = ["HandlerIloSetup", "generate_password"]
