"""Offline historical TargetDeliveryMap V1 verification only."""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import re
from datetime import datetime
from typing import Literal, Self, cast
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from omnimarket.rsd.v4_static_policy import (
    ContainerSecretSinkV1,
    TargetDeliveryValueKindV1,
    valkey_static_authority,
)

_SHA256 = r"^[0-9a-f]{64}$"
_COMMIT = r"^[0-9a-f]{40}$"
_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_IDENTIFIER = r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$"
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:[.][0-9]{1,6})?Z\Z"
)
_TARGET_DELIVERY_MAP_DOMAIN = b"omninode-rsd.target-delivery-map.sha256.v1\x00"
_URI_GRAMMAR_DOMAIN = b"omninode-rsd.runtime-uri-grammar.sha256.v1\x00"
_SIGNATURE_DOMAIN = b"omninode-rsd.target-delivery-map.ed25519.v1\x00"


class TargetDeliveryMapSigningError(ValueError):
    pass


class _Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, validate_default=True
    )


def _items(value: object, *, field: str) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise ValueError(f"{field} must be a tuple")
    return value


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(model: BaseModel, *, exclude: set[str] | None = None) -> bytes:
    return json.dumps(
        model.model_dump(mode="json", exclude=exclude or set(), warnings="error"),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _domain_sha256(domain: bytes, model: BaseModel) -> str:
    return _digest(domain + _canonical(model))


def _timestamp(value: str) -> datetime:
    if type(value) is not str or _TIMESTAMP.fullmatch(value) is None:
        raise ValueError("timestamp must be canonical UTC")
    return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")


def _authority(value: str, *, schemes: frozenset[str]) -> str:
    if type(value) is not str or any(c.isspace() for c in value):
        raise ValueError("authority is not canonical")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("authority is not canonical") from None
    if (
        parsed.scheme not in schemes
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path
        or parsed.hostname is None
        or port is None
    ):
        raise ValueError("authority is not canonical")
    return value


class ProviderReferenceV1(_Model):
    """Version-pinned metadata only; no provider value is represented."""

    provider: str = Field(pattern=_IDENTIFIER)
    service: str = Field(pattern=_IDENTIFIER)
    account: str = Field(pattern=_IDENTIFIER)
    version: int = Field(ge=1, le=1_000_000)
    reference_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def binds_metadata(self) -> Self:
        expected = _digest(
            json.dumps(
                {
                    "account": self.account,
                    "provider": self.provider,
                    "service": self.service,
                    "version": self.version,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        if self.reference_sha256 != expected:
            raise ValueError("provider reference hash does not bind metadata")
        return self

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.provider, self.service, self.account)


class ProviderReferencesV2(_Model):
    """Version-2 runtime provider references.

    This is the sole public aggregate reference model for the seven required
    provider items. There is deliberately no V1 alias or parser for the former
    six-item material set.
    """

    commitment_hmac: ProviderReferenceV1
    backup_encryption: ProviderReferenceV1
    encryption_key: ProviderReferenceV1
    auth_secret: ProviderReferenceV1
    primary_valkey_password: ProviderReferenceV1
    restore_valkey_password: ProviderReferenceV1
    postgres_application_password: ProviderReferenceV1
    tls_trust_anchor: ProviderReferenceV1 | None = None

    def all(self) -> tuple[ProviderReferenceV1, ...]:
        result = (
            self.commitment_hmac,
            self.backup_encryption,
            self.encryption_key,
            self.auth_secret,
            self.primary_valkey_password,
            self.restore_valkey_password,
            self.postgres_application_password,
        )
        return (
            result
            if self.tls_trust_anchor is None
            else (*result, self.tls_trust_anchor)
        )

    @model_validator(mode="after")
    def unique(self) -> Self:
        values = self.all()
        if len({value.identity for value in values}) != len(values):
            raise ValueError("provider items must be distinct")
        if len({value.reference_sha256 for value in values}) != len(values):
            raise ValueError("provider hashes must be distinct")
        return self


def _canonical_base64_bytes(value: str) -> bytes:
    """Accept only uniquely spelled standard base64 signatures."""

    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("signature base64 is invalid") from None
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("signature base64 is not canonical")
    return decoded


def _isolated_ipv4(value: str, *, field: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field} must be a canonical IPv4 literal")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise ValueError(f"{field} must be a canonical IPv4 literal") from None
    if (
        address.version != 4
        or not address.is_private
        or address.is_loopback
        or address.is_unspecified
        or address.is_multicast
        or address.is_link_local
        or str(address) != value
    ):
        raise ValueError(f"{field} must be a non-public IPv4 literal")
    return value


def _isolated_ipv4_network(value: str) -> str:
    if type(value) is not str:
        raise ValueError("network subnet must be canonical")
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError:
        raise ValueError("network subnet must be canonical") from None
    if (
        network.version != 4
        or not network.is_private
        or network.with_prefixlen != value
    ):
        raise ValueError("network subnet must be a non-public IPv4 CIDR")
    return value


class NetworkOptionV1(_Model):
    """A non-secret, exact Docker-network or volume option binding."""

    key: str = Field(pattern=_IDENTIFIER)
    value: str = Field(pattern=r"^[A-Za-z0-9_.:/=-]{1,256}$")


class IsolatedNetworkPlanV1(_Model):
    """One internal IPAM network allocated before runtime materialization."""

    name: str = Field(pattern=_IDENTIFIER)
    driver: Literal["bridge"]
    internal: Literal[True]
    subnet: str
    gateway: str
    options: tuple[NetworkOptionV1, ...] = Field(default=(), max_length=16)

    @field_validator("subnet")
    @classmethod
    def canonical_subnet(cls, value: str) -> str:
        return _isolated_ipv4_network(value)

    @field_validator("gateway")
    @classmethod
    def canonical_gateway(cls, value: str) -> str:
        return _isolated_ipv4(value, field="network gateway")

    @field_validator("options", mode="before")
    @classmethod
    def declared_options(cls, value: object) -> tuple[object, ...]:
        return _items(value, field="network options")

    @model_validator(mode="after")
    def exact_configuration(self) -> Self:
        network = ipaddress.ip_network(self.subnet)
        if ipaddress.ip_address(self.gateway) not in network:
            raise ValueError("network gateway must belong to subnet")
        pairs = tuple((option.key, option.value) for option in self.options)
        if pairs != tuple(sorted(pairs)) or len(set(pairs)) != len(pairs):
            raise ValueError("network options must be unique and canonical")
        return self


class AllocationVolumePlanV1(_Model):
    """One empty volume allocated before any cache container exists."""

    name: str = Field(pattern=_IDENTIFIER)
    driver: Literal["local"]
    options: tuple[NetworkOptionV1, ...] = Field(default=(), max_length=16)

    @field_validator("options", mode="before")
    @classmethod
    def declared_options(cls, value: object) -> tuple[object, ...]:
        return _items(value, field="volume options")

    @model_validator(mode="after")
    def exact_configuration(self) -> Self:
        pairs = tuple((option.key, option.value) for option in self.options)
        if pairs != tuple(sorted(pairs)) or len(set(pairs)) != len(pairs):
            raise ValueError("volume options must be unique and canonical")
        return self


class ComponentPlacementV1(_Model):
    """The single permitted attachment and static address of a runtime component."""

    component: Literal[
        "primary_infisical",
        "primary_valkey",
        "restore_infisical",
        "restore_valkey",
    ]
    network_name: str = Field(pattern=_IDENTIFIER)
    alias: str = Field(pattern=_IDENTIFIER)
    static_ipv4: str

    @field_validator("static_ipv4")
    @classmethod
    def canonical_address(cls, value: str) -> str:
        return _isolated_ipv4(value, field="component static IPv4")


class ExecutorPlacementV1(_Model):
    """A host-control-plane executor, never a disposable-network attachment.

    The executor controls Docker through the separately signed Unix-socket
    policy. It is not a container and therefore may not be smuggled into the
    allocation graph as a second attachment to either disposable network.
    """

    executor_id: str = Field(pattern=_IDENTIFIER)
    placement: Literal["host_control_plane_v1"]


class AllocationTopologyV2(_Model):
    """The complete allowed attachment graph; no component may join another network."""

    primary_network: IsolatedNetworkPlanV1
    restore_network: IsolatedNetworkPlanV1
    primary_infisical: ComponentPlacementV1
    primary_valkey: ComponentPlacementV1
    restore_infisical: ComponentPlacementV1
    restore_valkey: ComponentPlacementV1
    executor: ExecutorPlacementV1

    @model_validator(mode="after")
    def exact_pair_isolation(self) -> Self:
        primary = self.primary_network
        restore = self.restore_network
        placements = (
            self.primary_infisical,
            self.primary_valkey,
            self.restore_infisical,
            self.restore_valkey,
        )
        if primary.name == restore.name or ipaddress.ip_network(
            primary.subnet
        ).overlaps(ipaddress.ip_network(restore.subnet)):
            raise ValueError(
                "primary and restore networks must be distinct and non-overlapping"
            )
        if tuple(item.component for item in placements) != (
            "primary_infisical",
            "primary_valkey",
            "restore_infisical",
            "restore_valkey",
        ):
            raise ValueError("topology components are not canonical")
        if (
            self.primary_infisical.network_name != primary.name
            or self.primary_valkey.network_name != primary.name
            or self.restore_infisical.network_name != restore.name
            or self.restore_valkey.network_name != restore.name
        ):
            raise ValueError("component attachment escapes its isolated pair")
        addresses = tuple(item.static_ipv4 for item in placements)
        aliases = tuple(item.alias for item in placements)
        if len(set(addresses)) != len(addresses) or len(set(aliases)) != len(aliases):
            raise ValueError("component aliases and static addresses must be distinct")
        for placement, network in (
            (self.primary_infisical, primary),
            (self.primary_valkey, primary),
            (self.restore_infisical, restore),
            (self.restore_valkey, restore),
        ):
            address = ipaddress.ip_address(placement.static_ipv4)
            if (
                address not in ipaddress.ip_network(network.subnet)
                or placement.static_ipv4 == network.gateway
            ):
                raise ValueError(
                    "component static address does not belong to its isolated network"
                )
        if self.executor.placement != "host_control_plane_v1":
            raise ValueError("executor must remain a host control plane")
        return self


class PostgreSQLPreparedOperationV1(_Model):
    """One fixed psql stdin template and its value-free result projection."""

    operation_id: str = Field(pattern=_UUID)
    kind: Literal[
        "allocation_nologin_v1",
        "install_primary_scram_verifier_v1",
        "install_restore_scram_verifier_v1",
    ]
    psql_template_sha256: str = Field(pattern=_SHA256)
    result_projection_sha256: str = Field(pattern=_SHA256)
    stdin_protocol: Literal["postgresql_prepared_psql_stdin_v1"]
    secret_input: Literal[False, True]

    @model_validator(mode="after")
    def exact_operation_shape(self) -> Self:
        if (
            (self.kind == "allocation_nologin_v1" and self.secret_input is not False)
            or (
                self.kind
                in (
                    "install_primary_scram_verifier_v1",
                    "install_restore_scram_verifier_v1",
                )
                and self.secret_input is not True
            )
            or self.psql_template_sha256 == self.result_projection_sha256
        ):
            raise ValueError("PostgreSQL prepared operation is invalid")
        return self


class PostgreSQLScramVerifierInstallV1(_Model):
    """Non-secret protocol for a verifier-only PostgreSQL password transition.

    The executor may derive a verifier in bounded memory from the authorized
    application password, but this model never represents the password,
    verifier, SQL text, psql output, or a database URI.
    """

    schema_version: Literal["rsd.postgresql-scram-verifier-install.v1"]
    database_identity: Literal["primary_database", "restore_database"]
    prepared_operation_id: str = Field(pattern=_UUID)
    application_password_reference_sha256: str = Field(pattern=_SHA256)
    algorithm: Literal["scram-sha-256"]
    iterations: int = Field(ge=4096, le=1_000_000)
    salt_bytes: int = Field(ge=16, le=64)
    derivation_scope: Literal["executor_bounded_memory_v1"]
    sink: Literal["postgresql_prepared_psql_stdin_verifier_v1"]
    plaintext_to_psql_allowed: Literal[False]
    verifier_in_receipt_allowed: Literal[False]
    sql_in_receipt_allowed: Literal[False]
    output_in_receipt_allowed: Literal[False]
    logs_allowed: Literal[False]
    template_sha256: str = Field(pattern=_SHA256)


class PostgreSQLScramVerifierInstallsV1(_Model):
    """Independent verifier-only sinks for the primary and restore databases."""

    primary_database: PostgreSQLScramVerifierInstallV1
    restore_database: PostgreSQLScramVerifierInstallV1

    @model_validator(mode="after")
    def independent_sinks(self) -> Self:
        primary = self.primary_database
        restore = self.restore_database
        if (
            primary.database_identity != "primary_database"
            or restore.database_identity != "restore_database"
            or primary.prepared_operation_id == restore.prepared_operation_id
            or primary.template_sha256 == restore.template_sha256
        ):
            raise ValueError("PostgreSQL SCRAM verifier sinks are invalid")
        return self


class PostgreSQLLoginTransitionIntentV1(_Model):
    """Signed, observed-OID-bound authorization for the one login transition.

    The model intentionally states only role state and verifier presence.  It
    never carries a password, verifier text, SQL fragment, DSN, or URI.
    """

    schema_version: Literal["rsd.postgresql-login-transition-intent.v1"]
    transition_kind: Literal["enable_application_login_with_provider_verifier_v1"]
    database_identity: Literal["primary_database", "restore_database"]
    prepared_operation_id: str = Field(pattern=_UUID)
    system_identifier: str = Field(pattern=r"^[0-9]{8,32}$")
    database_name: str = Field(pattern=_IDENTIFIER)
    database_oid: int = Field(ge=1)
    schema_oid: int = Field(ge=1)
    owner_role: str = Field(pattern=_IDENTIFIER)
    owner_role_oid: int = Field(ge=1)
    application_role: str = Field(pattern=_IDENTIFIER)
    application_role_oid: int = Field(ge=1)
    application_password_reference_sha256: str = Field(pattern=_SHA256)
    prepared_control_policy_sha256: str = Field(pattern=_SHA256)
    scram_verifier_install: PostgreSQLScramVerifierInstallV1
    owner_can_login: Literal[False]
    owner_password_absent: Literal[True]
    application_can_login: Literal[True]
    application_password_verifier_installed: Literal[True]

    @model_validator(mode="after")
    def exact_secret_free_transition(self) -> Self:
        if (
            self.owner_role == self.application_role
            or self.owner_role_oid == self.application_role_oid
            or self.scram_verifier_install.database_identity != self.database_identity
            or self.scram_verifier_install.prepared_operation_id
            != self.prepared_operation_id
            or self.scram_verifier_install.application_password_reference_sha256
            != self.application_password_reference_sha256
        ):
            raise ValueError("PostgreSQL login transition is invalid")
        return self


class PostgreSQLLoginTransitionReceiptV1(_Model):
    """Value-free effect evidence for the one prepared login transition."""

    schema_version: Literal["rsd.postgresql-login-transition-receipt.v1"]
    database_identity: Literal["primary_database", "restore_database"]
    prepared_operation_id: str = Field(pattern=_UUID)
    system_identifier: str = Field(pattern=r"^[0-9]{8,32}$")
    database_name: str = Field(pattern=_IDENTIFIER)
    database_oid: int = Field(ge=1)
    schema_oid: int = Field(ge=1)
    owner_role: str = Field(pattern=_IDENTIFIER)
    owner_role_oid: int = Field(ge=1)
    application_role: str = Field(pattern=_IDENTIFIER)
    application_role_oid: int = Field(ge=1)
    application_password_reference_sha256: str = Field(pattern=_SHA256)
    prepared_control_policy_sha256: str = Field(pattern=_SHA256)
    prepared_operation_result_sha256: str = Field(pattern=_SHA256)
    owner_can_login: Literal[False]
    owner_password_absent: Literal[True]
    application_can_login: Literal[True]
    application_password_verifier_installed: Literal[True]

    @model_validator(mode="after")
    def exact_secret_free_receipt(self) -> Self:
        if (
            self.owner_role == self.application_role
            or self.owner_role_oid == self.application_role_oid
            or self.prepared_control_policy_sha256
            == self.prepared_operation_result_sha256
        ):
            raise ValueError("PostgreSQL login transition receipt is invalid")
        return self


def _canonical_uri_authority_suffix_utf8_byte_count(
    *, authority: str, scheme: str
) -> int:
    """Return the exact canonical authority suffix width for one URI grammar.

    ``urlsplit().hostname`` deliberately removes brackets from IPv6 literals;
    a rendered URI must retain those brackets.  Revalidate the authority and
    count the canonical suffix as it will appear on the wire instead of
    reconstructing a secret-bearing URI or relying on parsed host internals.
    """

    prefix = scheme + ":" + "//"
    canonical = _authority(authority, schemes=frozenset({scheme}))
    if not canonical.startswith(prefix):
        raise ValueError("URI grammar authority is invalid")
    return len(canonical.removeprefix(prefix).encode("utf-8"))


def postgresql_connection_uri_rendered_byte_count(
    *, authority: str, application_role: str, database_name: str
) -> int:
    """Return the exact URI size without ever assembling a provider value.

    The password placeholder has the fixed canonical Base64URL width of the
    approved application-password material.  This is a grammar commitment,
    not a URI and not a value-derived fingerprint.
    """

    authority_bytes = _canonical_uri_authority_suffix_utf8_byte_count(
        authority=authority, scheme="postgresql"
    )
    return (
        len(b"postgresql:")
        + len(b"//")
        + len(application_role.encode("utf-8"))
        + 1  # credential delimiter
        + 43  # canonical unpadded Base64URL application-password width
        + 1  # authority delimiter
        + authority_bytes
        + 1  # database path delimiter
        + len(database_name.encode("utf-8"))
    )


def valkey_connection_uri_rendered_byte_count(
    *, authority: str, database_index: int
) -> int:
    """Return the exact Valkey URI size without constructing its password."""

    authority_bytes = _canonical_uri_authority_suffix_utf8_byte_count(
        authority=authority, scheme="redis"
    )
    return (
        len(b"redis:")
        + len(b"//")
        + 1  # empty username delimiter
        + 43  # canonical unpadded Base64URL Valkey password width
        + 1  # authority delimiter
        + authority_bytes
        + 1  # database path delimiter
        + len(str(database_index).encode("ascii"))
    )


class PostgreSQLConnectionUriGrammarV1(_Model):
    """Value-free exact grammar for one target-only PostgreSQL URI.

    A future effect may construct this URI only in its bounded memory and
    place it only in the named target-process environment.  The model never
    stores, returns, fingerprints, or serializes the resulting URI.
    """

    schema_version: Literal["rsd.postgresql-connection-uri-grammar.v1"]
    database_identity: Literal["primary_database", "restore_database"]
    authority: str
    database_name: str = Field(pattern=_IDENTIFIER)
    application_role: str = Field(pattern=_IDENTIFIER)
    application_password_reference_sha256: str = Field(pattern=_SHA256)
    prepared_operation_id: str = Field(pattern=_UUID)
    target_process: Literal["primary_infisical", "restore_infisical"]
    environment_variable: Literal["DB_CONNECTION_URI"]
    uri_grammar: Literal["postgresql_user_password_authority_database_v1"]
    application_password_format: Literal[
        "postgres_application_password_base64url_32_v1"
    ]
    application_password_encoded_byte_count: Literal[43]
    rendered_uri_byte_count: int = Field(ge=1, le=1024)
    return_uri_allowed: Literal[False]
    persistent_storage_allowed: Literal[False]
    logging_allowed: Literal[False]
    public_artifact_allowed: Literal[False]

    @field_validator("authority")
    @classmethod
    def canonical_postgres(cls, value: str) -> str:
        return _authority(value, schemes=frozenset({"postgresql"}))

    @model_validator(mode="after")
    def exact_value_free_grammar(self) -> Self:
        expected_target = (
            "primary_infisical"
            if self.database_identity == "primary_database"
            else "restore_infisical"
        )
        if (
            self.target_process != expected_target
            or self.rendered_uri_byte_count
            != postgresql_connection_uri_rendered_byte_count(
                authority=self.authority,
                application_role=self.application_role,
                database_name=self.database_name,
            )
        ):
            raise ValueError("PostgreSQL URI grammar is invalid")
        return self


class ValkeyConnectionUriGrammarV1(_Model):
    """Value-free exact grammar for an Infisical-to-Valkey target URI."""

    schema_version: Literal["rsd.valkey-connection-uri-grammar.v1"]
    cache_identity: Literal["primary_valkey", "restore_valkey"]
    authority: str
    database_index: int = Field(ge=0, le=15)
    password_reference_sha256: str = Field(pattern=_SHA256)
    target_process: Literal["primary_infisical", "restore_infisical"]
    environment_variable: Literal["REDIS_URL"]
    uri_grammar: Literal["redis_password_authority_database_v1"]
    password_format: Literal["valkey_password_base64url_32_v1"]
    password_encoded_byte_count: Literal[43]
    rendered_uri_byte_count: int = Field(ge=1, le=1024)
    return_uri_allowed: Literal[False]
    persistent_storage_allowed: Literal[False]
    logging_allowed: Literal[False]
    public_artifact_allowed: Literal[False]

    @field_validator("authority")
    @classmethod
    def canonical_valkey(cls, value: str) -> str:
        return _authority(value, schemes=frozenset({"redis"}))

    @model_validator(mode="after")
    def exact_value_free_grammar(self) -> Self:
        expected_target = (
            "primary_infisical"
            if self.cache_identity == "primary_valkey"
            else "restore_infisical"
        )
        if (
            self.target_process != expected_target
            or self.rendered_uri_byte_count
            != valkey_connection_uri_rendered_byte_count(
                authority=self.authority, database_index=self.database_index
            )
        ):
            raise ValueError("Valkey URI grammar is invalid")
        return self


def runtime_connection_uri_grammar_sha256(
    grammar: PostgreSQLConnectionUriGrammarV1 | ValkeyConnectionUriGrammarV1,
) -> str:
    """Commit one value-free target URI grammar under its explicit domain.

    This helper intentionally accepts only the two concrete grammar models.
    It never renders a URI and therefore cannot acquire a copy of a password.
    """

    if type(grammar) not in {
        PostgreSQLConnectionUriGrammarV1,
        ValkeyConnectionUriGrammarV1,
    }:
        raise ValueError("runtime URI grammar is invalid")
    return _domain_sha256(_URI_GRAMMAR_DOMAIN, grammar)


class PostgreSQLRuntimeDatabaseIdentityV1(_Model):
    """One observed-OID-bound database transition and target URI grammar."""

    database_identity: Literal["primary_database", "restore_database"]
    observation_binding_sha256: str = Field(pattern=_SHA256)
    schema_oid: int = Field(ge=1)
    login_transition: PostgreSQLLoginTransitionIntentV1
    connection_uri: PostgreSQLConnectionUriGrammarV1

    @model_validator(mode="after")
    def exact_observed_identity(self) -> Self:
        if (
            self.login_transition.database_identity != self.database_identity
            or self.login_transition.schema_oid != self.schema_oid
            or self.connection_uri.database_identity != self.database_identity
            or self.connection_uri.database_name != self.login_transition.database_name
            or self.connection_uri.application_role
            != self.login_transition.application_role
            or self.connection_uri.application_password_reference_sha256
            != self.login_transition.application_password_reference_sha256
            or self.connection_uri.prepared_operation_id
            != self.login_transition.prepared_operation_id
        ):
            raise ValueError("PostgreSQL runtime identity is invalid")
        return self


class PostgreSQLRuntimeDatabaseIdentitiesV1(_Model):
    """The primary and restore identities must remain independent.

    The currently implemented allocation stage observes only the primary
    stage database.  A restore identity is still modeled separately here so a
    future restore observation cannot be replaced by the primary route.
    """

    primary_database: PostgreSQLRuntimeDatabaseIdentityV1
    restore_database: PostgreSQLRuntimeDatabaseIdentityV1

    @model_validator(mode="after")
    def independent_database_identities(self) -> Self:
        primary = self.primary_database.login_transition
        restore = self.restore_database.login_transition
        if (
            self.primary_database.database_identity != "primary_database"
            or self.restore_database.database_identity != "restore_database"
            or primary.database_name == restore.database_name
            or primary.database_oid == restore.database_oid
            or primary.application_role == restore.application_role
            or primary.application_role_oid == restore.application_role_oid
            or primary.prepared_operation_id == restore.prepared_operation_id
            or self.primary_database.observation_binding_sha256
            == self.restore_database.observation_binding_sha256
        ):
            raise ValueError("PostgreSQL runtime identities are invalid")
        return self


class ProviderMaterialFingerprintBindingV1(_Model):
    """One value-free material fingerprint admitted to a target delivery map."""

    purpose: Literal[
        "encryption_key",
        "auth_secret",
        "primary_valkey_password",
        "restore_valkey_password",
        "postgres_application_password",
    ]
    reference_sha256: str = Field(pattern=_SHA256)
    fingerprint_sha256: str = Field(pattern=_SHA256)


class TargetDeliveryFieldV1(_Model):
    """One ordered, value-free field sent to an exact target wrapper.

    ``derivation_binding_sha256`` is a grammar commitment, never a hash of a
    raw URI or a secret-bearing result.
    """

    ordinal: int = Field(ge=1, le=4)
    source_purpose: Literal[
        "encryption_key",
        "auth_secret",
        "primary_valkey_password",
        "restore_valkey_password",
        "postgres_application_password",
    ]
    source_reference_sha256: str = Field(pattern=_SHA256)
    source_fingerprint_sha256: str = Field(pattern=_SHA256)
    value_kind: TargetDeliveryValueKindV1
    target_field: Literal[
        "ENCRYPTION_KEY", "AUTH_SECRET", "DB_CONNECTION_URI", "REDIS_URL", "requirepass"
    ]
    format: Literal[
        "infisical_hex_16_v1",
        "infisical_auth_secret_base64_32_v1",
        "valkey_password_base64url_32_v1",
        "derived_postgresql_uri_v1",
        "derived_valkey_uri_v1",
    ]
    encoded_byte_count: int = Field(ge=1, le=1024)
    sink: ContainerSecretSinkV1
    derivation_binding_sha256: str = Field(pattern=_SHA256)
    persistence_allowed: Literal[False]
    logging_allowed: Literal[False]
    receipt_allowed: Literal[False]

    @field_validator("value_kind", mode="before")
    @classmethod
    def canonical_value_kind(cls, value: object) -> TargetDeliveryValueKindV1:
        if type(value) is TargetDeliveryValueKindV1:
            return value
        if type(value) is str:
            try:
                return TargetDeliveryValueKindV1(value)
            except ValueError:
                pass
        raise ValueError("target delivery value kind is invalid")

    @field_validator("sink", mode="before")
    @classmethod
    def canonical_sink(cls, value: object) -> ContainerSecretSinkV1:
        return _canonical_sink(value)


class ContainerTargetDeliveryV1(_Model):
    """One complete target-process or stdin-config delivery route."""

    component: Literal[
        "primary_infisical",
        "primary_valkey",
        "restore_infisical",
        "restore_valkey",
    ]
    derived_image_policy_sha256: str = Field(pattern=_SHA256)
    wrapper_artifact_binding_sha256: str = Field(pattern=_SHA256)
    attach_protocol_sha256: str = Field(pattern=_SHA256)
    sink: ContainerSecretSinkV1
    fields: tuple[TargetDeliveryFieldV1, ...] = Field(min_length=1, max_length=4)

    @field_validator("fields", mode="before")
    @classmethod
    def declared_fields(cls, value: object) -> tuple[object, ...]:
        return _items(value, field="target delivery fields")

    @field_validator("sink", mode="before")
    @classmethod
    def canonical_sink(cls, value: object) -> ContainerSecretSinkV1:
        return _canonical_sink(value)

    @model_validator(mode="after")
    def exact_target_route(self) -> Self:
        expected: dict[str, tuple[ContainerSecretSinkV1, tuple[str, ...]]] = {
            "primary_infisical": (
                ContainerSecretSinkV1.INFISICAL_TARGET_PROCESS_ENVIRONMENT,
                ("ENCRYPTION_KEY", "AUTH_SECRET", "DB_CONNECTION_URI", "REDIS_URL"),
            ),
            "restore_infisical": (
                ContainerSecretSinkV1.INFISICAL_TARGET_PROCESS_ENVIRONMENT,
                ("ENCRYPTION_KEY", "AUTH_SECRET", "DB_CONNECTION_URI", "REDIS_URL"),
            ),
            "primary_valkey": (
                ContainerSecretSinkV1.VALKEY_STDIN_CONFIGURATION,
                ("requirepass",),
            ),
            "restore_valkey": (
                ContainerSecretSinkV1.VALKEY_STDIN_CONFIGURATION,
                ("requirepass",),
            ),
        }
        if (
            type(self.sink) is not ContainerSecretSinkV1
            or self.sink != expected[self.component][0]
            or tuple(item.ordinal for item in self.fields)
            != tuple(range(1, len(self.fields) + 1))
            or tuple(item.target_field for item in self.fields)
            != expected[self.component][1]
            or any(item.sink is not self.sink for item in self.fields)
            or len(
                {
                    self.derived_image_policy_sha256,
                    self.wrapper_artifact_binding_sha256,
                    self.attach_protocol_sha256,
                }
            )
            != 3
        ):
            raise ValueError("container target delivery is invalid")
        return self


class TargetDeliveryMapV1(_Model):
    """Separately signed route map for all four processes and both databases.

    It contains only component identities, exact field grammar, provider
    metadata, and derivation commitments.  It cannot contain a secret,
    verifier, URI, command line, environment mapping, or target file.
    """

    schema_version: Literal["rsd.target-delivery-map.v1"]
    source_commit: str = Field(pattern=_COMMIT)
    allocation_intent_sha256: str = Field(pattern=_SHA256)
    topology: AllocationTopologyV2
    wrapper_manifest_sha256: str = Field(pattern=_SHA256)
    attach_protocol_sha256: str = Field(pattern=_SHA256)
    secret_handling_policy_sha256: str = Field(pattern=_SHA256)
    provider_references: ProviderReferencesV2
    material_fingerprints: tuple[
        ProviderMaterialFingerprintBindingV1,
        ProviderMaterialFingerprintBindingV1,
        ProviderMaterialFingerprintBindingV1,
        ProviderMaterialFingerprintBindingV1,
        ProviderMaterialFingerprintBindingV1,
    ]
    database_identities: PostgreSQLRuntimeDatabaseIdentitiesV1
    primary_valkey_connection_uri: ValkeyConnectionUriGrammarV1
    restore_valkey_connection_uri: ValkeyConnectionUriGrammarV1
    primary_infisical: ContainerTargetDeliveryV1
    primary_valkey: ContainerTargetDeliveryV1
    restore_infisical: ContainerTargetDeliveryV1
    restore_valkey: ContainerTargetDeliveryV1
    created_at: str
    signer_key_id: str = Field(pattern=_IDENTIFIER)
    signature_base64: str = Field(min_length=4, max_length=256)

    @field_validator("material_fingerprints", mode="before")
    @classmethod
    def declared_fingerprints(cls, value: object) -> tuple[object, ...]:
        return _items(value, field="target delivery material fingerprints")

    @field_validator("created_at")
    @classmethod
    def canonical_created_at(cls, value: str) -> str:
        _timestamp(value)
        return value

    @model_validator(mode="after")
    def exact_complete_map(self) -> Self:
        targets = (
            self.primary_infisical,
            self.primary_valkey,
            self.restore_infisical,
            self.restore_valkey,
        )
        fingerprints = self.material_fingerprints
        expected_purposes = (
            "encryption_key",
            "auth_secret",
            "primary_valkey_password",
            "restore_valkey_password",
            "postgres_application_password",
        )
        references = {
            "encryption_key": self.provider_references.encryption_key.reference_sha256,
            "auth_secret": self.provider_references.auth_secret.reference_sha256,
            "primary_valkey_password": (
                self.provider_references.primary_valkey_password.reference_sha256
            ),
            "restore_valkey_password": (
                self.provider_references.restore_valkey_password.reference_sha256
            ),
            "postgres_application_password": (
                self.provider_references.postgres_application_password.reference_sha256
            ),
        }
        fields = tuple(field for target in targets for field in target.fields)
        expected_source_purposes: dict[str, tuple[str, ...]] = {
            "primary_infisical": (
                "encryption_key",
                "auth_secret",
                "postgres_application_password",
                "primary_valkey_password",
            ),
            "restore_infisical": (
                "encryption_key",
                "auth_secret",
                "postgres_application_password",
                "restore_valkey_password",
            ),
            "primary_valkey": ("primary_valkey_password",),
            "restore_valkey": ("restore_valkey_password",),
        }
        by_component = {str(target.component): target for target in targets}
        primary_uri = self.database_identities.primary_database.connection_uri
        restore_uri = self.database_identities.restore_database.connection_uri
        primary_valkey_uri = self.primary_valkey_connection_uri
        restore_valkey_uri = self.restore_valkey_connection_uri
        topology = self.topology
        fingerprint_by_purpose = {item.purpose: item for item in fingerprints}
        expected_fields: dict[str, tuple[tuple[object, ...], ...]] = {
            "primary_infisical": (
                (
                    "encryption_key",
                    TargetDeliveryValueKindV1.DIRECT_PROVIDER_MATERIAL,
                    "infisical_hex_16_v1",
                    32,
                    ContainerSecretSinkV1.INFISICAL_TARGET_PROCESS_ENVIRONMENT,
                    fingerprint_by_purpose.get("encryption_key"),
                ),
                (
                    "auth_secret",
                    TargetDeliveryValueKindV1.DIRECT_PROVIDER_MATERIAL,
                    "infisical_auth_secret_base64_32_v1",
                    44,
                    ContainerSecretSinkV1.INFISICAL_TARGET_PROCESS_ENVIRONMENT,
                    fingerprint_by_purpose.get("auth_secret"),
                ),
                (
                    "postgres_application_password",
                    TargetDeliveryValueKindV1.DERIVED_POSTGRESQL_URI,
                    "derived_postgresql_uri_v1",
                    primary_uri.rendered_uri_byte_count,
                    ContainerSecretSinkV1.INFISICAL_TARGET_PROCESS_ENVIRONMENT,
                    primary_uri,
                ),
                (
                    "primary_valkey_password",
                    TargetDeliveryValueKindV1.DERIVED_VALKEY_URI,
                    "derived_valkey_uri_v1",
                    primary_valkey_uri.rendered_uri_byte_count,
                    ContainerSecretSinkV1.INFISICAL_TARGET_PROCESS_ENVIRONMENT,
                    primary_valkey_uri,
                ),
            ),
            "restore_infisical": (
                (
                    "encryption_key",
                    TargetDeliveryValueKindV1.DIRECT_PROVIDER_MATERIAL,
                    "infisical_hex_16_v1",
                    32,
                    ContainerSecretSinkV1.INFISICAL_TARGET_PROCESS_ENVIRONMENT,
                    fingerprint_by_purpose.get("encryption_key"),
                ),
                (
                    "auth_secret",
                    TargetDeliveryValueKindV1.DIRECT_PROVIDER_MATERIAL,
                    "infisical_auth_secret_base64_32_v1",
                    44,
                    ContainerSecretSinkV1.INFISICAL_TARGET_PROCESS_ENVIRONMENT,
                    fingerprint_by_purpose.get("auth_secret"),
                ),
                (
                    "postgres_application_password",
                    TargetDeliveryValueKindV1.DERIVED_POSTGRESQL_URI,
                    "derived_postgresql_uri_v1",
                    restore_uri.rendered_uri_byte_count,
                    ContainerSecretSinkV1.INFISICAL_TARGET_PROCESS_ENVIRONMENT,
                    restore_uri,
                ),
                (
                    "restore_valkey_password",
                    TargetDeliveryValueKindV1.DERIVED_VALKEY_URI,
                    "derived_valkey_uri_v1",
                    restore_valkey_uri.rendered_uri_byte_count,
                    ContainerSecretSinkV1.INFISICAL_TARGET_PROCESS_ENVIRONMENT,
                    restore_valkey_uri,
                ),
            ),
            "primary_valkey": (
                (
                    "primary_valkey_password",
                    TargetDeliveryValueKindV1.DIRECT_PROVIDER_MATERIAL,
                    "valkey_password_base64url_32_v1",
                    43,
                    ContainerSecretSinkV1.VALKEY_STDIN_CONFIGURATION,
                    fingerprint_by_purpose.get("primary_valkey_password"),
                ),
            ),
            "restore_valkey": (
                (
                    "restore_valkey_password",
                    TargetDeliveryValueKindV1.DIRECT_PROVIDER_MATERIAL,
                    "valkey_password_base64url_32_v1",
                    43,
                    ContainerSecretSinkV1.VALKEY_STDIN_CONFIGURATION,
                    fingerprint_by_purpose.get("restore_valkey_password"),
                ),
            ),
        }

        def field_matches(
            field: TargetDeliveryFieldV1, expected: tuple[object, ...]
        ) -> bool:
            purpose, value_kind, field_format, byte_count, sink, binding = expected
            if (
                field.source_purpose != purpose
                or field.value_kind is not value_kind
                or field.format != field_format
                or field.encoded_byte_count != byte_count
                or field.sink is not sink
            ):
                return False
            if type(binding) is ProviderMaterialFingerprintBindingV1:
                return (
                    field.source_reference_sha256 == binding.reference_sha256
                    and field.source_fingerprint_sha256 == binding.fingerprint_sha256
                    and field.derivation_binding_sha256 == binding.fingerprint_sha256
                )
            if type(binding) is PostgreSQLConnectionUriGrammarV1:
                return (
                    field.derivation_binding_sha256
                    == runtime_connection_uri_grammar_sha256(binding)
                )
            if type(binding) is ValkeyConnectionUriGrammarV1:
                return (
                    field.derivation_binding_sha256
                    == runtime_connection_uri_grammar_sha256(binding)
                )
            return False

        if (
            tuple(item.component for item in targets)
            != (
                "primary_infisical",
                "primary_valkey",
                "restore_infisical",
                "restore_valkey",
            )
            or tuple(item.purpose for item in fingerprints) != expected_purposes
            or len({item.reference_sha256 for item in fingerprints}) != 5
            or len({item.fingerprint_sha256 for item in fingerprints}) != 5
            or any(
                item.reference_sha256 != references[item.purpose]
                for item in fingerprints
            )
            or any(
                tuple(field.source_purpose for field in by_component[component].fields)
                != expected_source_purposes[component]
                for component in expected_source_purposes
            )
            or any(
                field.source_reference_sha256 != references[field.source_purpose]
                for field in fields
            )
            or any(
                field.source_fingerprint_sha256
                != fingerprint_by_purpose[field.source_purpose].fingerprint_sha256
                for field in fields
            )
            or primary_valkey_uri.cache_identity != "primary_valkey"
            or restore_valkey_uri.cache_identity != "restore_valkey"
            or primary_valkey_uri.authority
            != valkey_static_authority(topology.primary_valkey.static_ipv4)
            or restore_valkey_uri.authority
            != valkey_static_authority(topology.restore_valkey.static_ipv4)
            or primary_valkey_uri.password_reference_sha256
            != references["primary_valkey_password"]
            or restore_valkey_uri.password_reference_sha256
            != references["restore_valkey_password"]
            or any(
                not field_matches(field, expected)
                for component, target in by_component.items()
                for field, expected in zip(
                    target.fields, expected_fields[component], strict=True
                )
            )
            or len(_canonical_base64_bytes(self.signature_base64)) != 64
        ):
            raise ValueError("target delivery map is invalid")
        return self


def target_delivery_map_sha256(delivery_map: TargetDeliveryMapV1) -> str:
    """Return the signed map commitment used by intents, receipts, and journals."""

    if type(delivery_map) is not TargetDeliveryMapV1:
        raise ValueError("target delivery map is invalid")
    return _domain_sha256(_TARGET_DELIVERY_MAP_DOMAIN, delivery_map)


def _canonical_sink(value: object) -> ContainerSecretSinkV1:
    if type(value) is ContainerSecretSinkV1:
        return value
    if type(value) is str:
        try:
            return ContainerSecretSinkV1(value)
        except ValueError:
            pass
    raise ValueError("container secret sink is invalid")


class TargetDeliveryMapSignerTrustAnchorV1(_Model):
    schema_version: Literal["rsd.target-delivery-map-signer-trust-anchor.v1"]
    key_id: str = Field(pattern=_IDENTIFIER)
    public_key_base64: str = Field(min_length=4, max_length=128)
    public_key_fingerprint_sha256: str = Field(pattern=_SHA256)
    algorithm: Literal["ed25519"]

    @model_validator(mode="after")
    def exact_public_key(self) -> Self:
        key = _canonical_base64_bytes(self.public_key_base64)
        if len(key) != 32 or _digest(key) != self.public_key_fingerprint_sha256:
            raise ValueError("target delivery map signer anchor is invalid")
        return self


_MISSING_MODEL_STATE = object()


def _exact_state(value: object, expected: type[BaseModel], active: set[int]) -> None:
    """Reject constructed, hidden, deleted, cyclic, or type-drifted state."""
    if type(value) is not expected or id(value) in active:
        raise ValueError("model is invalid")
    state = getattr(value, "__dict__", _MISSING_MODEL_STATE)
    if (
        type(state) is not dict
        or set(cast(dict[str, object], state)) != set(expected.model_fields)
        or getattr(value, "__pydantic_extra__", _MISSING_MODEL_STATE) is not None
        or getattr(value, "__pydantic_" + "pri" + "vate__", _MISSING_MODEL_STATE)
        is not None
        or type(getattr(value, "__pydantic_fields_set__", _MISSING_MODEL_STATE))
        is not set
        or not value.__pydantic_fields_set__.issubset(expected.model_fields)
    ):
        raise ValueError("model is invalid")
    active.add(id(value))
    try:
        for item in cast(dict[str, object], state).values():
            _exact_value(item, active)
    finally:
        active.remove(id(value))


def _exact_value(value: object, active: set[int]) -> None:
    if isinstance(value, BaseModel):
        _exact_state(value, type(value), active)
    elif type(value) in (tuple, list, set, frozenset):
        if id(value) in active:
            raise ValueError("model is invalid")
        active.add(id(value))
        try:
            for item in cast(tuple[object, ...], value):
                _exact_value(item, active)
        finally:
            active.remove(id(value))
    elif type(value) is dict:
        if id(value) in active:
            raise ValueError("model is invalid")
        active.add(id(value))
        try:
            for key, item in cast(dict[object, object], value).items():
                _exact_value(key, active)
                _exact_value(item, active)
        finally:
            active.remove(id(value))


def _same_exact_value(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, BaseModel):
        return all(
            _same_exact_value(getattr(left, name), getattr(right, name))
            for name in left.__class__.model_fields
        )
    if type(left) is tuple:
        right_tuple = cast(tuple[object, ...], right)
        return len(left) == len(right_tuple) and all(
            _same_exact_value(a, b) for a, b in zip(left, right_tuple, strict=True)
        )
    return left == right


def _strict[T: _Model](value: object, expected: type[T]) -> T:
    if type(value) is not expected:
        raise ValueError("model type is invalid")
    _exact_state(value, expected, set())
    payload = _canonical(cast(BaseModel, value))
    result = expected.model_validate(
        _arrays_to_tuples(json.loads(payload.decode("ascii"))), strict=True
    )
    if _canonical(result) != payload or not _same_exact_value(value, result):
        raise ValueError("model is invalid")
    return result


def historical_target_delivery_map_v1_canonical_json(
    delivery_map: TargetDeliveryMapV1,
) -> bytes:
    return _canonical(_strict(delivery_map, TargetDeliveryMapV1))


def _arrays_to_tuples(value: object) -> object:
    if type(value) is list:
        return tuple(_arrays_to_tuples(item) for item in value)
    if type(value) is dict:
        return {key: _arrays_to_tuples(item) for key, item in value.items()}
    return value


def parse_historical_target_delivery_map_v1_canonical_json(
    payload: bytes,
) -> TargetDeliveryMapV1:
    if type(payload) is not bytes or not payload or not payload.isascii():
        raise ValueError("target delivery map is invalid")
    raw = json.loads(payload.decode("ascii"))
    result = TargetDeliveryMapV1.model_validate(_arrays_to_tuples(raw), strict=True)
    if _canonical(result) != payload:
        raise ValueError("target delivery map is invalid")
    return result


def target_delivery_map_v1_canonical_message(
    delivery_map: TargetDeliveryMapV1,
) -> bytes:
    return _SIGNATURE_DOMAIN + _canonical(
        _strict(delivery_map, TargetDeliveryMapV1), exclude={"signature_base64"}
    )


def verify_target_delivery_map_v1_signature(
    *,
    delivery_map: TargetDeliveryMapV1,
    signer_trust_anchor: TargetDeliveryMapSignerTrustAnchorV1,
) -> TargetDeliveryMapV1:
    try:
        canonical = _strict(delivery_map, TargetDeliveryMapV1)
        anchor = _strict(signer_trust_anchor, TargetDeliveryMapSignerTrustAnchorV1)
        if canonical.signer_key_id != anchor.key_id:
            raise ValueError
        Ed25519PublicKey.from_public_bytes(
            _canonical_base64_bytes(anchor.public_key_base64)
        ).verify(
            _canonical_base64_bytes(canonical.signature_base64),
            target_delivery_map_v1_canonical_message(canonical),
        )
        return canonical
    except (InvalidSignature, TypeError, ValueError):
        raise TargetDeliveryMapSigningError(
            "target delivery map signature validation failed"
        ) from None
