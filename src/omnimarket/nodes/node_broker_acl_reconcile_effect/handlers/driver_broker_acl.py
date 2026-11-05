# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""List, diff, and add declared broker ACLs with credentials kept on the broker host."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

RESOURCE_TYPES = frozenset({"TOPIC", "GROUP", "CLUSTER", "TRANSACTIONAL_ID"})
PATTERNS = frozenset({"LITERAL", "PREFIXED"})
OPERATIONS = frozenset(
    {
        "READ",
        "WRITE",
        "DESCRIBE",
        "DELETE",
        "IDEMPOTENT_WRITE",
        "CREATE",
        "DESCRIBE_CONFIGS",
    }
)
RESOURCE_FLAGS = {
    "TOPIC": "--topic",
    "GROUP": "--group",
    "CLUSTER": "--cluster",
    "TRANSACTIONAL_ID": "--transactional-id",
}
RemoteRunner = Callable[[str, str], str]


class DeclarationError(ValueError):
    """A declaration or broker listing is malformed."""


@dataclass(frozen=True, order=True)
class AclGrant:
    """An ALLOW grant for all hosts, validated before any broker operation."""

    principal: str
    resource_type: str
    pattern: str
    name: str
    operation: str

    def __post_init__(self) -> None:
        if not self.principal.startswith("User:") or self.principal in {
            "User:",
            "User:*",
        }:
            raise DeclarationError(f"invalid principal: {self.principal!r}")
        if self.resource_type not in RESOURCE_TYPES:
            raise DeclarationError(f"invalid resource_type: {self.resource_type!r}")
        if self.pattern not in PATTERNS:
            raise DeclarationError(f"invalid pattern: {self.pattern!r}")
        if self.operation not in OPERATIONS:
            raise DeclarationError(f"invalid operation: {self.operation!r}")
        if self.operation == "DELETE" and self.resource_type != "GROUP":
            raise DeclarationError("DELETE is allowed on GROUP only")
        if self.operation == "IDEMPOTENT_WRITE" and self.resource_type != "CLUSTER":
            raise DeclarationError("IDEMPOTENT_WRITE is allowed on CLUSTER only")
        if self.resource_type == "TRANSACTIONAL_ID" and self.operation not in {
            "WRITE",
            "DESCRIBE",
        }:
            raise DeclarationError("TRANSACTIONAL_ID allows WRITE and DESCRIBE only")
        if not self.name or self.name == "*":
            raise DeclarationError(f"invalid resource name: {self.name!r}")
        if self.resource_type == "CLUSTER" and (
            self.name != "kafka-cluster" or self.pattern != "LITERAL"
        ):
            raise DeclarationError(
                "CLUSTER grants must be LITERAL kafka-cluster grants"
            )


@dataclass(frozen=True)
class AclConnection:
    """Declaration connection cells and the explicitly configured SSH target."""

    lane: str
    host: str
    compose_project: str
    broker_container: str
    superuser_source_container: str
    ssh_target: str
    timeout_seconds: float = 120

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any], target: str, timeout: float = 120
    ) -> AclConnection:
        """Require every connection cell before constructing a remote command."""
        values = {
            key: _required_string(raw, key)
            for key in (
                "lane",
                "host",
                "compose_project",
                "broker_container",
                "superuser_source_container",
            )
        }
        if not target or target.startswith("-"):
            raise DeclarationError(
                "an SSH target is required and must not start with '-'"
            )
        return cls(**values, ssh_target=target, timeout_seconds=timeout)


@dataclass(frozen=True)
class AclDeclaration:
    """A validated connection and immutable declaration."""

    connection: AclConnection
    grants: tuple[AclGrant, ...]


def _required_string(mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise DeclarationError(f"{key} must be a non-empty string")
    return value


def load_principals(raw: object) -> list[AclGrant]:
    """Validate principal grant blocks before constructing grants."""
    if not isinstance(raw, list) or not raw:
        raise DeclarationError("principals must be a non-empty list")
    grants: list[AclGrant] = []
    for block in raw:
        if not isinstance(block, dict) or set(block) != {"principal", "grants"}:
            raise DeclarationError("principal must contain principal and grants")
        principal = _required_string(block, "principal")
        rows = block["grants"]
        if not isinstance(rows, list) or not rows:
            raise DeclarationError(
                f"principal {principal!r} must have at least one grant"
            )
        for row in rows:
            if not isinstance(row, dict) or set(row) != {
                "resource_type",
                "pattern",
                "name",
                "operation",
            }:
                raise DeclarationError(
                    "grant must contain resource_type, pattern, name and operation"
                )
            values = {key: _required_string(row, key) for key in row}
            grants.append(AclGrant(principal=principal, **values))
    return grants


def load_declaration(path: Path, target: str) -> AclDeclaration:
    """Read and validate a standalone declaration before contacting a broker."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise DeclarationError("declaration is not valid YAML") from error
    if not isinstance(raw, dict):
        raise DeclarationError("declaration must be a mapping")
    grants = load_principals(raw.get("principals"))
    if len(set(grants)) != len(grants):
        raise DeclarationError("declaration contains a duplicate grant")
    return AclDeclaration(AclConnection.from_mapping(raw, target), tuple(grants))


def parse_rpk_acl_list(text: str) -> frozenset[AclGrant]:
    """Parse relevant ALLOW-any-host rows from ``rpk security acl list`` output."""
    header = (
        "PRINCIPAL",
        "HOST",
        "RESOURCE-TYPE",
        "RESOURCE-NAME",
        "RESOURCE-PATTERN-TYPE",
        "OPERATION",
        "PERMISSION",
        "ERROR",
    )
    header_found = False
    grants: set[AclGrant] = set()
    for line in text.splitlines():
        columns = line.split()
        if tuple(columns) == header:
            header_found = True
            continue
        if not header_found or not columns:
            continue
        if len(columns) < 7:
            raise DeclarationError(f"invalid rpk ACL row: {line!r}")
        principal, host, resource_type, name, pattern, operation, permission = columns[
            :7
        ]
        if permission == "ALLOW" and host == "*":
            grants.add(_live_grant(principal, resource_type, pattern, name, operation))
    if not header_found:
        raise DeclarationError("rpk ACL list output is missing its header")
    return frozenset(grants)


def diff(
    declared: frozenset[AclGrant], live: frozenset[AclGrant], principals: frozenset[str]
) -> tuple[tuple[AclGrant, ...], tuple[AclGrant, ...]]:
    """Return declared grants missing from live state and live extras for managed principals."""
    missing = tuple(sorted(declared - live))
    undeclared = tuple(
        sorted(
            grant
            for grant in live
            if grant.principal in principals and grant not in declared
        )
    )
    return missing, undeclared


def rpk_create_args(grant: AclGrant) -> list[str]:
    """Build rpk ACL-create arguments after the ``rpk`` executable."""
    grant.__post_init__()
    resource = [RESOURCE_FLAGS[grant.resource_type]]
    if grant.resource_type != "CLUSTER":
        resource.append(grant.name)
    return [
        "security",
        "acl",
        "create",
        "--allow-principal",
        grant.principal,
        "--operation",
        grant.operation.lower(),
        *resource,
        "--resource-pattern-type",
        grant.pattern.lower(),
    ]


def build_remote_script(decl: AclConnection, rpk_argvs: list[list[str]]) -> str:
    """Build a remote script which obtains broker credentials only on the host."""
    source = shlex.quote(decl.superuser_source_container)
    broker = shlex.quote(decl.broker_container)
    env_format = shlex.quote("{{range .Config.Env}}{{println .}}{{end}}")
    lines = [
        "set -eu",
        f'U="$(docker inspect {source} --format {env_format} | '
        "sed -n 's/^DEV_KAFKA_SASL_USERNAME=//p')\"",
        f'P="$(docker inspect {source} --format {env_format} | '
        "sed -n 's/^DEV_KAFKA_SASL_PASSWORD=//p')\"",
        # An empty credential would reach the broker unauthenticated and fail
        # there with a misleading error; stop here and say which source failed.
        f'[ -n "$U" ] && [ -n "$P" ] || {{ echo "no lane superuser credentials in '
        f'{source}" >&2; exit 3; }}',
    ]
    for rpk_argv in rpk_argvs:
        quoted_argv = " ".join(shlex.quote(argument) for argument in rpk_argv)
        lines.append(
            f"docker exec {broker} rpk {quoted_argv} "
            "-X brokers=redpanda:9092 -X sasl.mechanism=SCRAM-SHA-256 "
            '-X user="$U" -X pass="$P"'
        )
    return "\n".join(lines) + "\n"


def _grant_line(prefix: str, grant: AclGrant) -> str:
    return " ".join(
        (
            prefix,
            grant.principal,
            grant.resource_type,
            grant.pattern,
            grant.name,
            grant.operation,
        )
    )


def _live_grant(
    principal: str, resource_type: str, pattern: str, name: str, operation: str
) -> AclGrant:
    """Retain even undeclared wildcard or unsupported grants from broker listings."""
    grant = object.__new__(AclGrant)
    for key, value in zip(
        ("principal", "resource_type", "pattern", "name", "operation"),
        (principal, resource_type, pattern, name, operation),
        strict=True,
    ):
        object.__setattr__(grant, key, value)
    return grant


def run_remote(target: str, script: str, *, timeout: float = 120) -> str:
    """Execute a remote script without fetching broker credentials locally."""
    completed = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", target, "sh", "-s"],
        input=script,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            f"remote ACL command failed (exit {completed.returncode}): {completed.stderr.strip()[-400:]}"
        )
    return completed.stdout


def apply(
    grants: Iterable[AclGrant],
    connection: AclConnection,
    mode: Literal["dry_run", "apply"],
    runner: RemoteRunner = run_remote,
) -> str:
    """Report the diff, add missing grants in apply mode, and report broker readback."""
    if mode not in {"dry_run", "apply"}:
        raise DeclarationError(f"invalid mode: {mode!r}")
    declared = frozenset(grants)
    for grant in declared:
        grant.__post_init__()
    principals = frozenset(grant.principal for grant in declared)
    listing = [
        ["security", "acl", "list", "--allow-principal", principal]
        for principal in sorted(principals)
    ]
    reports: list[str] = []

    def read() -> tuple[tuple[AclGrant, ...], tuple[AclGrant, ...]]:
        live = parse_rpk_acl_list(
            runner(connection.ssh_target, build_remote_script(connection, listing))
        )
        missing, undeclared = diff(declared, live, principals)
        reports.append(
            f"lane={connection.lane} declared={len(declared)} live={len(live)} missing={len(missing)} undeclared={len(undeclared)}"
        )
        reports.extend(_grant_line("MISSING", grant) for grant in missing)
        reports.extend(_grant_line("UNDECLARED", grant) for grant in undeclared)
        return missing, undeclared

    missing, _ = read()
    if mode == "dry_run":
        reports.extend(_grant_line("CREATE", grant) for grant in missing)
    elif missing:
        runner(
            connection.ssh_target,
            build_remote_script(
                connection, [rpk_create_args(grant) for grant in missing]
            ),
        )
        read()
    return "\n".join(reports) + "\n"
