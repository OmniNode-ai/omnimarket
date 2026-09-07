"""Inert canonical V4 support models, extracted from RSD e853400."""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA256 = r"^[0-9a-f]{64}$"
_IDENTIFIER = r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$"
_CONTAINER_ENVIRONMENT_CONSTRUCTION_V2_DOMAIN = (
    b"omninode-rsd.container-environment-construction.sha256.v2\x00"
)
_VALKEY_STATIC_CONFIGURATION_V2_DOMAIN = (
    b"omninode-rsd.valkey-static-configuration.sha256.v2\x00"
)
_VALKEY_SCHEME = "redis:"
_VALKEY_FIXED_PORT = 6379


class _Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, validate_default=True
    )


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _domain_sha256(domain: bytes, value: BaseModel) -> str:
    return _digest(
        domain
        + json.dumps(
            value.model_dump(mode="json", warnings="error"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _items(value: object, *, field: str) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise ValueError(f"{field} must be a tuple")
    return value


def _isolated_ipv4(value: str, *, field: str) -> str:
    try:
        address = ipaddress.IPv4Address(value)
    except (ipaddress.AddressValueError, ValueError):
        raise ValueError(f"{field} is invalid") from None
    if str(address) != value or not address.is_private:
        raise ValueError(f"{field} is invalid")
    return value


def _canonical_base64_bytes(value: str) -> bytes:
    """Accept only uniquely spelled standard base64 signatures."""

    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("signature base64 is invalid") from None
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("signature base64 is not canonical")
    return decoded


def valkey_static_authority(static_ipv4: str) -> str:
    """Render the one canonical planned Valkey authority without a credential.

    Static component placement is an IPv4-only allocation contract.  Keeping
    the scheme and port constants separate also prevents a caller from
    treating the URI grammar as an arbitrary listener selection.
    """

    if type(static_ipv4) is not str:
        raise ValueError("Valkey static authority is invalid")
    try:
        address = ipaddress.IPv4Address(static_ipv4)
    except ipaddress.AddressValueError:
        raise ValueError("Valkey static authority is invalid") from None
    return f"{_VALKEY_SCHEME}//{address}:{_VALKEY_FIXED_PORT}"


class ContainerSecretSinkV1(StrEnum):
    """The only non-secret value sinks permitted to a container bootstrap."""

    INFISICAL_TARGET_PROCESS_ENVIRONMENT = "infisical_target_process_environment_v1"
    VALKEY_STDIN_CONFIGURATION = "valkey_stdin_configuration_v1"


class TargetDeliveryValueKindV1(StrEnum):
    """A local attach field is either direct material or a bounded derivation."""

    DIRECT_PROVIDER_MATERIAL = "direct_provider_material_v1"
    DERIVED_POSTGRESQL_URI = "derived_postgresql_uri_v1"
    DERIVED_VALKEY_URI = "derived_valkey_uri_v1"


class ContainerAttachTicketTrustAnchorV1(_Model):
    """Pinned public verification key embedded in one V2 wrapper profile."""

    schema_version: Literal["rsd.container-attach-ticket-trust-anchor.v1"]
    key_id: str = Field(pattern=_IDENTIFIER)
    public_key_base64: str = Field(min_length=4, max_length=128)
    public_key_fingerprint_sha256: str = Field(pattern=_SHA256)
    algorithm: Literal["ed25519"]

    @model_validator(mode="after")
    def exact_public_key(self) -> Self:
        public_key = _canonical_base64_bytes(self.public_key_base64)
        if (
            len(public_key) != 32
            or _digest(public_key) != self.public_key_fingerprint_sha256
        ):
            raise ValueError("container attach ticket trust anchor is invalid")
        return self


class ContainerBootstrapStaticEnvironmentEntryV2(_Model):
    """One exact non-secret image environment entry allowed after create."""

    name: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    value: str = Field(min_length=0, max_length=1024)

    @field_validator("value")
    @classmethod
    def safe_static_value(cls, value: str) -> str:
        if (
            type(value) is not str
            or "\x00" in value
            or any(character in value for character in "\r\n")
        ):
            raise ValueError("container static environment value is invalid")
        return value

    @property
    def rendered(self) -> str:
        """Render the only Docker image-env spelling for this non-secret entry."""

        return f"{self.name}={self.value}"


class ContainerBootstrapStaticEnvironmentV2(_Model):
    """Sealed exact image environment; create-time ``Config.Env`` stays empty."""

    schema_version: Literal["rsd.container-bootstrap-static-environment.v2"]
    entries: tuple[ContainerBootstrapStaticEnvironmentEntryV2, ...] = Field(
        max_length=64
    )
    environment_sha256: str = Field(pattern=_SHA256)
    target_delivery_fields_forbidden: Literal[True]
    inherited_environment_allowed: Literal[False]

    @field_validator("entries", mode="before")
    @classmethod
    def declared_entries(cls, value: object) -> tuple[object, ...]:
        return _items(value, field="container static environment entries")

    @model_validator(mode="after")
    def exact_nonsecret_environment(self) -> Self:
        forbidden = {
            "ENCRYPTION_KEY",
            "AUTH_SECRET",
            "DB_CONNECTION_URI",
            "REDIS_URL",
            "requirepass",
        }
        rendered = tuple(item.rendered for item in self.entries)
        if (
            tuple(sorted(item.name for item in self.entries))
            != tuple(item.name for item in self.entries)
            or len({item.name for item in self.entries}) != len(self.entries)
            or any(item.name in forbidden for item in self.entries)
            or self.environment_sha256
            != _digest(json.dumps(rendered, separators=(",", ":")).encode("utf-8"))
        ):
            raise ValueError("container static environment is invalid")
        return self


class ContainerBootstrapEnvironmentConstructionPolicyV2(_Model):
    """Exact wrapper-to-child environment boundary for one V2 target.

    Docker's create-time ``Config.Env`` and a base image's static environment
    are not a child-process authorization channel.  The future wrapper must
    clear its ambient process environment and construct one new ``envp`` from
    this signed non-secret allowlist plus the exact V1 target-delivery fields.
    This model deliberately carries no delivered value or URI.
    """

    schema_version: Literal[
        "rsd.container-bootstrap-environment-construction-policy.v2"
    ]
    component: Literal[
        "primary_infisical",
        "primary_valkey",
        "restore_infisical",
        "restore_valkey",
    ]
    host_environment_allowed: Literal[False]
    env_file_allowed: Literal[False]
    docker_config_environment_allowed: Literal[False]
    inherited_environment_cleared_before_child_exec: Literal[True]
    inherited_environment_read_allowed: Literal[False]
    inherited_environment_pass_through_allowed: Literal[False]
    explicit_child_envp_required: Literal[True]
    global_setenv_for_target_values_allowed: Literal[False]
    static_entries: tuple[ContainerBootstrapStaticEnvironmentEntryV2, ...] = Field(
        max_length=32
    )
    static_environment_sha256: str = Field(pattern=_SHA256)
    image_static_environment_sha256: str = Field(pattern=_SHA256)
    dynamic_target_field_names: tuple[str, ...] = Field(max_length=4)
    wrapper_network_client_allowed: Literal[False]
    telemetry_environment_allowed: Literal[False]
    target_value_in_argv_allowed: Literal[False]
    target_value_in_file_allowed: Literal[False]
    target_value_in_logs_allowed: Literal[False]

    @field_validator("static_entries", "dynamic_target_field_names", mode="before")
    @classmethod
    def declared_sequences(cls, value: object) -> tuple[object, ...]:
        return _items(value, field="container child environment V2 sequence")

    @model_validator(mode="after")
    def exact_child_environment_boundary(self) -> Self:
        expected_dynamic: dict[str, tuple[str, ...]] = {
            "primary_infisical": (
                "ENCRYPTION_KEY",
                "AUTH_SECRET",
                "DB_CONNECTION_URI",
                "REDIS_URL",
            ),
            "restore_infisical": (
                "ENCRYPTION_KEY",
                "AUTH_SECRET",
                "DB_CONNECTION_URI",
                "REDIS_URL",
            ),
            "primary_valkey": (),
            "restore_valkey": (),
        }
        forbidden_static_names = {
            "AUTH_SECRET",
            "DB_CONNECTION_URI",
            "DOCKER_HOST",
            "ENCRYPTION_KEY",
            "ENV_FILE",
            "HOME",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "NODE_OPTIONS",
            "PATH",
            "REDIS_URL",
            "requirepass",
        }
        rendered = tuple(item.rendered for item in self.static_entries)
        if (
            tuple(sorted(item.name for item in self.static_entries))
            != tuple(item.name for item in self.static_entries)
            or len({item.name for item in self.static_entries})
            != len(self.static_entries)
            or any(item.name in forbidden_static_names for item in self.static_entries)
            or self.static_environment_sha256
            != _digest(json.dumps(rendered, separators=(",", ":")).encode("utf-8"))
            or self.dynamic_target_field_names != expected_dynamic[self.component]
        ):
            raise ValueError("container child environment V2 policy is invalid")
        return self


def container_bootstrap_environment_construction_policy_sha256(
    policy: ContainerBootstrapEnvironmentConstructionPolicyV2,
) -> str:
    """Return the signed-profile commitment for a child ``envp`` boundary."""

    if type(policy) is not ContainerBootstrapEnvironmentConstructionPolicyV2:
        raise ValueError("container child environment V2 policy is invalid")
    return _domain_sha256(_CONTAINER_ENVIRONMENT_CONSTRUCTION_V2_DOMAIN, policy)


class ContainerBootstrapFdPolicyV2(_Model):
    """Fixed wrapper/child file-descriptor topology; no output carrier exists."""

    schema_version: Literal["rsd.container-bootstrap-fd-policy.v2"]
    wrapper_stdin: Literal["engine_attach_stdin_v2"]
    wrapper_stdout: Literal["engine_attach_stdout_protocol_only_v2"]
    wrapper_stderr: Literal["dev_null"]
    infisical_child_stdin: Literal["dev_null"]
    valkey_child_stdin: Literal["private_wrapper_config_pipe"]
    child_stdout: Literal["dev_null"]
    child_stderr: Literal["dev_null"]
    exec_status_pipe: Literal["private_cloexec_status_pipe"]
    output_fd_secret_allowed: Literal[False]
    logging_allowed: Literal[False]


class ContainerBootstrapPid1PolicyV2(_Model):
    """Exact future PID-1 behavior, distinct from application readiness."""

    schema_version: Literal["rsd.container-bootstrap-pid1-policy.v2"]
    signal_order: tuple[Literal["SIGTERM"], Literal["SIGINT"]]
    target_process_group_required: Literal[True]
    forwards_signals_to_process_group: Literal[True]
    reaps_all_children: Literal[True]
    propagates_target_leader_exit_status: Literal[True]
    terminal_ack_before_exit_required: Literal[True]
    child_process_readiness_distinct: Literal[True]
    service_readiness_distinct: Literal[True]
    shutdown_timeout_seconds: int = Field(ge=1, le=300)

    @field_validator("signal_order", mode="before")
    @classmethod
    def declared_signals(cls, value: object) -> tuple[object, ...]:
        return _items(value, field="container wrapper V2 signals")

    @model_validator(mode="after")
    def exact_pid1_policy(self) -> Self:
        if self.signal_order != ("SIGTERM", "SIGINT"):
            raise ValueError("container wrapper V2 PID1 policy is invalid")
        return self


class ContainerBootstrapMemorySafetyPolicyV2(_Model):
    """Honest wrapper-owned memory controls, without impossible erase claims."""

    schema_version: Literal["rsd.container-bootstrap-memory-safety-policy.v2"]
    wrapper_owned_mutable_buffers_only: Literal[True]
    mlock_required_before_secret_delivery: Literal[True]
    core_dumps_disabled: Literal[True]
    dumpable_disabled: Literal[True]
    panic_or_backtrace_logging_allowed: Literal[False]
    wrapper_staging_buffers_zeroized: Literal[True]
    kernel_socket_buffer_zeroization_claimed: Literal[False]
    kernel_pipe_buffer_zeroization_claimed: Literal[False]
    swap_zeroization_claimed: Literal[False]
    target_process_memory_zeroization_claimed: Literal[False]


class ContainerBootstrapValkeyLaunchPolicyV2(_Model):
    """Complete static Valkey profile; only ``requirepass`` remains dynamic.

    The exact command allowlist itself is represented by a pinned digest and
    mandatory negative-test evidence because this public contract does not
    invent a version-specific Infisical command subset.  A future wrapper must
    refuse delivery unless that sealed allowlist has separately been proven for
    the pinned Valkey build.
    """

    schema_version: Literal["rsd.container-bootstrap-valkey-launch-policy.v2"]
    command: tuple[Literal["valkey-server"], Literal["-"]]
    stdin_configuration_required: Literal[True]
    dynamic_environment_allowed: Literal[False]
    dynamic_argv_allowed: Literal[False]
    data_mount_target: Literal["/data"]
    data_mount_read_only: Literal[True]
    persistence_disabled: Literal[True]
    config_file_allowed: Literal[False]
    wrapper_only_password_assembly: Literal[True]
    isolated_bind_address: str
    listener_port: Literal[6379]
    protected_mode: Literal["yes"]
    daemonize: Literal["no"]
    save_schedule: Literal[""]
    appendonly: Literal["no"]
    shutdown_on_sigint: Literal["nosave"]
    shutdown_on_sigterm: Literal["nosave"]
    logfile: Literal["/dev/null"]
    loglevel: Literal["nothing"]
    syslog_enabled: Literal["no"]
    crash_log_enabled: Literal["no"]
    set_proc_title: Literal["no"]
    requirepass_directive_count: Literal[1]
    requirepass_raw_byte_count: Literal[32]
    requirepass_canonical_base64url_unpadded: Literal[True]
    requirepass_dynamic_directive: Literal["requirepass"]
    requirepass_grammar: Literal[
        "ascii_base64url_32_no_whitespace_controls_quotes_config_delimiters_v2"
    ]
    acl_profile_sha256: str = Field(pattern=_SHA256)
    acl_pinned_version_sha256: str = Field(pattern=_SHA256)
    acl_command_allowlist_sha256: str = Field(pattern=_SHA256)
    acl_negative_test_evidence_sha256: str = Field(pattern=_SHA256)
    acl_denied_command_categories: tuple[str, ...]
    static_directive_order: tuple[str, ...]
    static_configuration_sha256: str = Field(pattern=_SHA256)

    @field_validator("command", mode="before")
    @classmethod
    def declared_command(cls, value: object) -> tuple[object, ...]:
        return _items(value, field="Valkey V2 command")

    @field_validator(
        "acl_denied_command_categories", "static_directive_order", mode="before"
    )
    @classmethod
    def declared_static_sequences(cls, value: object) -> tuple[object, ...]:
        return _items(value, field="Valkey V2 static configuration sequence")

    @field_validator("isolated_bind_address")
    @classmethod
    def isolated_address(cls, value: str) -> str:
        return _isolated_ipv4(value, field="Valkey V2 isolated bind address")

    @model_validator(mode="after")
    def exact_valkey_command(self) -> Self:
        expected_denials = (
            "acl_administration",
            "configuration",
            "debug_and_module_administration",
            "persistence",
            "replication_configuration",
            "shutdown",
        )
        expected_directives = (
            "bind",
            "port",
            "protected-mode",
            "daemonize",
            "save",
            "appendonly",
            "shutdown-on-sigint",
            "shutdown-on-sigterm",
            "logfile",
            "loglevel",
            "syslog-enabled",
            "crash-log-enabled",
            "set-proc-title",
            "acl-profile",
            "requirepass",
        )
        if (
            self.command != ("valkey-server", "-")
            or self.acl_denied_command_categories != expected_denials
            or self.static_directive_order != expected_directives
            or len(
                {
                    self.acl_profile_sha256,
                    self.acl_pinned_version_sha256,
                    self.acl_command_allowlist_sha256,
                    self.acl_negative_test_evidence_sha256,
                }
            )
            != 4
            or self.static_configuration_sha256
            != container_bootstrap_valkey_static_configuration_sha256(self)
        ):
            raise ValueError("Valkey V2 launch policy is invalid")
        return self


def container_bootstrap_valkey_static_configuration_sha256(
    policy: ContainerBootstrapValkeyLaunchPolicyV2,
) -> str:
    """Commit the ordered public Valkey stdin configuration without its password."""

    if type(policy) is not ContainerBootstrapValkeyLaunchPolicyV2:
        raise ValueError("Valkey V2 launch policy is invalid")
    return _domain_sha256(
        _VALKEY_STATIC_CONFIGURATION_V2_DOMAIN,
        policy.model_copy(update={"static_configuration_sha256": "0" * 64}),
    )
