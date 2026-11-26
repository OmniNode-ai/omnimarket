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

from omnimarket.models.broker_acl_reconcile import validate_superuser_env_file

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


CREDENTIAL_MISSING_EXIT = 3
#: First word of the one stderr line the script prologue prints before it exits ``CREDENTIAL_MISSING_EXIT``.
CREDENTIAL_MISSING_MARKER = "onex-broker-credential-missing"
CREDENTIAL_NAMES = ("DEV_KAFKA_SASL_USERNAME", "DEV_KAFKA_SASL_PASSWORD")
DEFAULT_SUPERUSER_ENV_FILE = "~/.omnibase/.env"


class DeclarationError(ValueError):
    """A declaration or broker listing is malformed."""


class BrokerCredentialMissingError(RuntimeError):
    """A DEV_KAFKA_SASL name is absent or empty in the declared secret source.

    The message names the key and the source file, never a value.
    """


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
    # The declared secret source: an env file on the broker host holding the dev superuser
    # credential under the DEV_KAFKA_SASL_* names (the operator env file the deploy agent
    # unit declares as DEPLOY_AGENT_ENV_FILE). ``superuser_source_container`` stays a
    # required legacy cell of the declaration shape and is no longer read.
    superuser_env_file: str = DEFAULT_SUPERUSER_ENV_FILE

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
        env_file = raw.get("superuser_env_file", DEFAULT_SUPERUSER_ENV_FILE)
        _remote_path(env_file)
        return cls(
            **values,
            ssh_target=target,
            timeout_seconds=timeout,
            superuser_env_file=env_file,
        )


@dataclass(frozen=True)
class AclDeclaration:
    """A validated connection and immutable declaration."""

    connection: AclConnection
    grants: tuple[AclGrant, ...]


def _remote_path(path: object) -> str:
    """Quote a declared env file path for the broker host, expanding only a leading ``~/``."""
    try:
        checked = validate_superuser_env_file(path)
    except ValueError as exc:
        raise DeclarationError(str(exc)) from None
    path = checked
    if path.startswith("~/"):
        return f'"$HOME"/{shlex.quote(path[2:])}'
    return shlex.quote(path)


@dataclass(frozen=True)
class RemovalScope:
    """The resource types and name prefixes inside which a reconcile may remove a row."""

    resource_types: frozenset[str]
    name_prefixes: tuple[str, ...]

    @classmethod
    def from_mapping(cls, raw: object) -> RemovalScope:
        """Read the contract's removal scope, refusing one that cannot gate a removal."""
        if not isinstance(raw, Mapping):
            raise DeclarationError("reconcile_scope must be a mapping")
        resource_types = raw.get("resource_types")
        name_prefixes = raw.get("name_prefixes")
        if not isinstance(resource_types, list) or not resource_types:
            raise DeclarationError(
                "reconcile_scope resource_types must be a non-empty list"
            )
        if not isinstance(name_prefixes, list) or not name_prefixes:
            raise DeclarationError(
                "reconcile_scope name_prefixes must be a non-empty list"
            )
        for resource_type in resource_types:
            if (
                not isinstance(resource_type, str)
                or resource_type not in RESOURCE_TYPES
            ):
                raise DeclarationError(f"invalid resource_type: {resource_type!r}")
        for prefix in name_prefixes:
            if not isinstance(prefix, str) or not prefix:
                raise DeclarationError(
                    "reconcile_scope name_prefixes entries must be non-empty strings"
                )
        return cls(frozenset(resource_types), tuple(name_prefixes))

    def covers(self, grant: AclGrant) -> bool:
        return grant.resource_type in self.resource_types and any(
            grant.name.startswith(prefix) for prefix in self.name_prefixes
        )


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


def split_undeclared(
    undeclared: Iterable[AclGrant],
    scope: RemovalScope,
    managed_principals: frozenset[str],
    only_names: frozenset[str] = frozenset(),
) -> tuple[tuple[AclGrant, ...], tuple[AclGrant, ...], tuple[AclGrant, ...]]:
    """Split undeclared rows into removable, out-of-scope, and held (a name kept by request).

    A row of a login outside managed_principals is out of scope like a row outside the scope's
    resource types and name prefixes: reported, never removed. With no managed logins at all
    nothing is removable.
    """
    removable: list[AclGrant] = []
    out_of_scope: list[AclGrant] = []
    held: list[AclGrant] = []
    for grant in sorted(undeclared):
        if grant.principal not in managed_principals or not scope.covers(grant):
            out_of_scope.append(grant)
        elif only_names and grant.name not in only_names:
            held.append(grant)
        else:
            removable.append(grant)
    return tuple(removable), tuple(out_of_scope), tuple(held)


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


def rpk_delete_args(grant: AclGrant) -> list[str]:
    """Build rpk ACL-delete arguments naming every field, so exactly one row is removed."""
    grant.__post_init__()
    resource = [RESOURCE_FLAGS[grant.resource_type]]
    if grant.resource_type != "CLUSTER":
        resource.append(grant.name)
    return [
        "security",
        "acl",
        "delete",
        "--allow-principal",
        grant.principal,
        "--allow-host",
        "*",
        "--operation",
        grant.operation.lower(),
        *resource,
        "--resource-pattern-type",
        grant.pattern.lower(),
        "--no-confirm",
    ]


def build_remote_script(decl: AclConnection, rpk_argvs: list[list[str]]) -> str:
    """Build a remote script which obtains broker credentials only on the host.

    The credential is read by name from the declared env file on the broker host, never from a
    container: an exited one-shot container is removed by host cleanup and cannot be relied on.
    A missing file, name or empty value stops the script with exit ``CREDENTIAL_MISSING_EXIT``
    and one stderr line that starts with ``CREDENTIAL_MISSING_MARKER`` and names the key and the
    file, never a value. Only those branches print the marker, so an exit 3 from ``docker exec``,
    rpk or a later script line carries no marker and is not read as a missing credential.

    The file's real consumer sources it as shell (the deploy agent launcher), so the reader
    takes the last ``[export ]NAME=value`` assignment and strips what the shell would: CR, one
    level of surrounding quotes, and the trailing whitespace and `` # comment`` the shell drops.
    ``NAME = value`` is not a shell assignment and is not read. A value is read only in one of
    three forms, whole: ``'...'`` or ``"..."`` followed by nothing, whitespace or a `` # comment``,
    or a bare word of no quote, whitespace, ``;&|<>()``, tilde, backslash, dollar or backtick,
    followed the same way. Any other form the shell reads differently from this reader (text after a
    closing quote, a quote or an operator in a bare word, inner whitespace that starts no comment,
    a leading space after the equals sign, and inside double quotes or a bare word a backslash, a
    dollar sign or a backtick) fails closed with the same exit and marker, never a guessed or
    truncated value.
    """
    env_file = _remote_path(decl.superuser_env_file)
    broker = shlex.quote(decl.broker_container)
    user_name, pass_name = CREDENTIAL_NAMES
    marker = CREDENTIAL_MISSING_MARKER
    # The last ``[export ]NAME=`` line, with only the CR stripped: ``raw`` is what the shell would parse.
    raw = (
        "tr -d '\\r' < \"$F\" | "
        "sed -n -e 's/^[[:space:]]*export[[:space:]][[:space:]]*//' "
        "-e 's/^[[:space:]]*{name}=//p' | tail -n 1"
    )
    # Strip what the shell strips from a value ``check_form`` has already accepted.
    strip = (
        'sed -e \'s/^"\\([^"]*\\)".*$/\\1/\' -e t '
        "-e 's/^'\\''\\([^'\\'']*\\)'\\''.*$/\\1/' -e t "
        "-e 's/[[:space:]][[:space:]]*#.*$//' -e 's/[[:space:]]*$//'"
    )
    # Forms the shell consumer reads differently from this reader are refused, never guessed at or
    # cut short: a leading space (``NAME= v`` runs ``v`` with NAME empty), and any value that is not,
    # whole, one of three accepted shapes. Single quotes are literal; double quotes and a bare word
    # may not hold a backslash, dollar or backtick (the shell decodes or expands them), and a bare
    # word may hold no quote, whitespace, ``;&|<>()`` or tilde (the shell expands ``~`` to a home
    # directory at the start of a value and after a colon). After a closing quote or a bare word
    # only whitespace, or whitespace then ``#`` and a comment, may follow.
    tail = r"([[:space:]]*|[[:space:]]+#.*)$"
    accepted = " || ".join(
        f"printf '%s\\n' \"$R_{{var}}\" | grep -Eq {shlex.quote(pattern)}"
        for pattern in (
            "^'[^']*'" + tail,
            '^"[^"$`\\]*"' + tail,
            "^[^[:space:]'\"$`\\;&|<>()~]+" + tail,
        )
    )
    check_form = (
        f'case "$R_{{var}}" in\n'
        f'  [[:space:]]*) echo "{marker} {{name}} has an unreadable form (leading whitespace) in $F" >&2; '
        f"exit {CREDENTIAL_MISSING_EXIT};;\n"
        "  '') ;;\n"
        f'  *) {accepted} || {{{{ echo "{marker} {{name}} has an unreadable form (text the shell reads '
        f'differently) in $F" >&2; exit {CREDENTIAL_MISSING_EXIT}; }}}};;\n'
        "esac"
    )
    lines = [
        "set -eu",
        f"F={env_file}",
        f'[ -r "$F" ] || {{ echo "{marker} no readable declared secret source $F" >&2; '
        f"exit {CREDENTIAL_MISSING_EXIT}; }}",
        f'R_U="$({raw.format(name=user_name)})"',
        f'R_P="$({raw.format(name=pass_name)})"',
        check_form.format(var="U", name=user_name),
        check_form.format(var="P", name=pass_name),
        f'U="$(printf \'%s\\n\' "$R_U" | {strip})"',
        f'P="$(printf \'%s\\n\' "$R_P" | {strip})"',
        # An empty credential would reach the broker unauthenticated and fail
        # there with a misleading error; stop here and say which key is missing.
        f'[ -n "$U" ] || {{ echo "{marker} {user_name} missing or empty in $F" >&2; '
        f"exit {CREDENTIAL_MISSING_EXIT}; }}",
        f'[ -n "$P" ] || {{ echo "{marker} {pass_name} missing or empty in $F" >&2; '
        f"exit {CREDENTIAL_MISSING_EXIT}; }}",
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
    if completed.returncode == CREDENTIAL_MISSING_EXIT:
        # Typed only when the prologue itself said so; the message is that marker line alone.
        marked = [
            line.strip()
            for line in completed.stderr.splitlines()
            if line.strip().startswith(CREDENTIAL_MISSING_MARKER)
        ]
        if marked:
            raise BrokerCredentialMissingError(marked[-1][:400])
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


def reconcile(
    grants: Iterable[AclGrant],
    connection: AclConnection,
    mode: Literal["dry_run", "reconcile"],
    runner: RemoteRunner = run_remote,
    scope: RemovalScope | None = None,
    only_names: frozenset[str] = frozenset(),
    expected_removals: int | None = None,
    known: frozenset[AclGrant] = frozenset(),
    managed_principals: frozenset[str] = frozenset(),
) -> str:
    """Report the diff and every planned removal; reconcile mode adds the missing rows, removes
    the removable ones after matching the expected count, and reports the broker readback.
    A row a scoped overlay holds (known) is never created, never counted as declared, never
    removed; the listing covers its logins too. Only a managed login's row is removable, and
    without a scope no row is: every undeclared row is reported out of scope."""
    if mode not in {"dry_run", "reconcile"}:
        raise DeclarationError(f"invalid mode: {mode!r}")
    declared = frozenset(grants)
    for grant in declared | known:
        grant.__post_init__()
    principals = (
        frozenset(grant.principal for grant in declared | known) | managed_principals
    )
    listing = [
        ["security", "acl", "list", "--allow-principal", principal]
        for principal in sorted(principals)
    ]
    removals = scope if scope is not None else RemovalScope(frozenset(), ())
    reports: list[str] = []

    def read() -> tuple[
        frozenset[AclGrant], tuple[AclGrant, ...], tuple[AclGrant, ...]
    ]:
        live = parse_rpk_acl_list(
            runner(connection.ssh_target, build_remote_script(connection, listing))
        )
        missing, undeclared = diff(declared, live, principals)
        # A row a scoped overlay holds is never undeclared: never created, never removed.
        undeclared = tuple(grant for grant in undeclared if grant not in known)
        reports.append(
            f"lane={connection.lane} declared={len(declared)} live={len(live)} missing={len(missing)} undeclared={len(undeclared)} known={len(known)}"
        )
        reports.extend(_grant_line("MISSING", grant) for grant in missing)
        reports.extend(_grant_line("UNDECLARED", grant) for grant in undeclared)
        return live, missing, undeclared

    _, missing, undeclared = read()
    removable, out_of_scope, held = split_undeclared(
        undeclared, removals, managed_principals, only_names
    )
    reports.extend(_grant_line("REMOVE", grant) for grant in removable)
    reports.extend(_grant_line("OUT_OF_SCOPE", grant) for grant in out_of_scope)
    reports.extend(_grant_line("HELD", grant) for grant in held)
    if mode == "dry_run":
        reports.extend(_grant_line("CREATE", grant) for grant in missing)
        return "\n".join(reports) + "\n"
    if expected_removals != len(removable):
        raise DeclarationError(
            f"expected {expected_removals} removals, found {len(removable)}; nothing changed"
        )
    if missing:
        runner(
            connection.ssh_target,
            build_remote_script(
                connection, [rpk_create_args(grant) for grant in missing]
            ),
        )
    if removable:
        runner(
            connection.ssh_target,
            build_remote_script(
                connection, [rpk_delete_args(grant) for grant in removable]
            ),
        )
    live, _, _ = read()
    reports.extend(
        _grant_line("REMOVED", grant) for grant in removable if grant not in live
    )
    return "\n".join(reports) + "\n"
