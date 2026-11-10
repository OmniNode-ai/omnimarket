# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The full-suite pytest host decision, moved out of the pre-push guard script (OMN-23367).

The script keeps the reads (git, files, environment, grants) and the pytest hook; every rule about which
host and which login may run a full suite sits here and takes bytes in and gives a decision out.
"""

import re

from omnimarket.nodes.node_pytest_full_suite_host_guard.models.model_pytest_full_suite_host_guard import (
    ModelPytestFullSuiteHostGuardDecision,
    ModelPytestFullSuiteHostGuardRequest,
)

UV_COLUMN = 5
WORKROOT_COLUMN = 7
_MODE_COLUMN = 11
_ROW_COLUMNS = 15

# Legacy env override -> host-table row label. An override REPLACES the row it names; it never ADDS a
# hostname to the designated set. That distinction is load-bearing: under a table listing several hosts,
# an override that merely appended a name could no longer DE-designate the local machine, silently
# inverting the OMN-15059 guard that `test_guard_refuses_full_suite_escalation_on_non_200_host` proves
# by forcing a nonsense hostname.
_LEGACY_OVERRIDE_BY_LABEL = {
    "h200": "PREPUSH_200_HOSTNAME",
    "h201c": "PREPUSH_201_GATE_RUNNER_HOSTNAME",
}


def override_var(label: str) -> str:
    """Per-row override env var name, matching prepush_dispatch.sh."""
    sanitized = "".join(c if c.isalnum() else "_" for c in label.upper())
    return f"PREPUSH_HOST_OVERRIDE_{sanitized}"


def login_overlay_block(overlay_text: str) -> str:
    """The ``table: |`` block of the per-login overlay file."""
    lines = overlay_text.splitlines()
    start = next(
        (i for i, ln in enumerate(lines) if re.fullmatch(r"table:\s*\|[-+]?\s*", ln)),
        None,
    )
    if start is None:
        raise ValueError("per-login host overlay has no `table: |` block")
    block: list[str] = []
    for ln in lines[start + 1 :]:
        if ln.strip() and not ln[:1].isspace():
            break
        block.append(ln.strip())
    return "\n".join(block)


def parse_login_rows(block: str) -> dict[tuple[str, str], tuple[str, str]]:
    """``label login uv_abs_path workroot`` rows keyed by (label, login)."""
    rows: dict[tuple[str, str], tuple[str, str]] = {}
    for raw in block.splitlines():
        fields = raw.split("#", 1)[0].split()
        if not fields:
            continue
        if len(fields) != 4 or not all(f.startswith("/") for f in fields[2:]):
            raise ValueError(
                f"per-login host row must be `label login uv_abs_path workroot` "
                f"with absolute paths: {raw!r}"
            )
        key = (fields[0], fields[1])
        if key in rows:
            raise ValueError(f"per-login host row repeated for {key[0]} {key[1]}")
        rows[key] = (fields[2], fields[3])
    return rows


def apply_login_rows(
    table_text: str, rows: dict[tuple[str, str], tuple[str, str]], login: str
) -> str:
    """Replace uv_abs_path and workroot of the rows ``login`` owns; nothing else moves."""
    if not login:
        return table_text
    out: list[str] = []
    for line in table_text.split("\n"):
        fields = line.split("\t")
        if len(fields) >= _ROW_COLUMNS and not line.lstrip().startswith("#"):
            owned = rows.get((fields[0], login))
            if owned:
                fields[UV_COLUMN], fields[WORKROOT_COLUMN] = owned
                line = "\t".join(fields)
        out.append(line)
    return "\n".join(out)


def designated_hostnames(
    table_text: str, env_overrides: dict[str, str]
) -> tuple[str, ...]:
    """Authorizing identities of the table, with the row overrides applied."""
    names: list[str] = []
    for line in table_text.splitlines():
        fields = line.split("#", 1)[0].split("\t")
        if len(fields) <= _MODE_COLUMN or fields[_MODE_COLUMN] != "authorizing":
            continue
        label, hostname = fields[0], fields[2]
        legacy = _LEGACY_OVERRIDE_BY_LABEL.get(label)
        if legacy and env_overrides.get(legacy):
            hostname = env_overrides[legacy]
        names.append(env_overrides.get(override_var(label)) or hostname)
    return tuple(names)


def authorizing_row(table_text: str, host: str) -> list[str] | None:
    """The fields of the authorizing row whose hostname is ``host``, or None."""
    for line in table_text.splitlines():
        fields = line.split("#", 1)[0].split("\t")
        if (
            len(fields) >= _ROW_COLUMNS
            and fields[2].lower() == host.lower()
            and fields[_MODE_COLUMN] == "authorizing"
        ):
            return fields
    return None


def login_decision(table_text: str, *, host: str, login: str) -> str:
    """One line naming the guard's decision for a login on a host and what it resolves to."""
    fields = authorizing_row(table_text, host)
    if fields is None:
        return (
            f"prepush-guard decision: host={host} login={login} verdict=refused "
            "reason=not-designated"
        )
    return (
        f"prepush-guard decision: host={host} login={login} row={fields[0]} "
        f"verdict=authorized uv={fields[UV_COLUMN]} workroot={fields[WORKROOT_COLUMN]}"
    )


def is_full_suite_target(
    *,
    args: list[str],
    testpaths: list[str],
    keyword: str,
    markexpr: str,
    full_suite_target: str,
) -> bool:
    """An unnarrowed full-suite root or ancestor; narrow paths and filters are exempt."""
    if keyword or markexpr:
        return False
    normalized_full = full_suite_target.rstrip("/") + "/"
    return any(
        normalized_full.startswith(str(raw).rstrip("/") + "/")
        for raw in args or testpaths
    )


def full_suite_host_violation_message(
    *,
    host: str,
    target_hostname: str,
    additional_target_hostnames: tuple[str, ...] = (),
    override_authorized: bool,
    private_table_rel: str,
    host_table_rel: str,
) -> str | None:
    """Pure host decision; unknown identity stays fail-open and grants permit mismatches."""
    if not host or override_authorized:
        return None
    allowed_hostnames = tuple(
        name for name in (target_hostname, *additional_target_hostnames) if name
    )
    if any(host.lower() == allowed.lower() for allowed in allowed_hostnames):
        return None
    reason = (
        "no gate host is configured. The shipped table carries only a disabled localhost row"
        if not allowed_hostnames
        else "not a designated gate host carrying mode=authorizing in the host tables "
        f"({', '.join(repr(allowed) for allowed in allowed_hostnames)})"
    )
    return (
        f"direct full-suite pytest invocation refused on host '{host}': {reason}. "
        "Create the fifteen-column deployment table at "
        f"${{ONEX_WORKSPACE_CONFIG_ROOT}}/{private_table_rel} and set "
        "ONEX_WORKSPACE_CONFIG_ROOT to its root, then run from a configured gate host "
        f"(schema and procedure: {host_table_rel}). OR mint a single-use override "
        "grant to run here anyway (visible, receipted, degraded-evidence -- do not "
        "use as a routine bypass): `uv run python scripts/hooks/prepush_override_grant.py "
        "mint --reason '<why>'`."
    )


def table_status_line(
    present_path: str | None, private_table_rel: str, host_table_rel: str
) -> str:
    """Where the deployment host table is, or that it is not configured."""
    if present_path:
        return f"deployment host table: {present_path}"
    return (
        f"deployment host table NOT CONFIGURED: {private_table_rel} is absent -- set "
        f"ONEX_WORKSPACE_CONFIG_ROOT to a root holding {private_table_rel} "
        f"(fifteen columns, see {host_table_rel})"
    )


class HandlerPytestFullSuiteHostGuard:
    def handle(
        self, request: ModelPytestFullSuiteHostGuardRequest
    ) -> ModelPytestFullSuiteHostGuardDecision:
        table = request.table_text
        if request.login_overlay_text is not None:
            rows = parse_login_rows(login_overlay_block(request.login_overlay_text))
            table = apply_login_rows(table, rows, request.login)
        names = designated_hostnames(table, request.env_overrides)
        fields = authorizing_row(table, request.host)
        message = full_suite_host_violation_message(
            host=request.host,
            target_hostname=names[0] if names else "",
            additional_target_hostnames=names[1:],
            override_authorized=False,
            private_table_rel=request.private_table_rel,
            host_table_rel=request.host_table_rel,
        )
        return ModelPytestFullSuiteHostGuardDecision(
            allowed=message is None,
            verdict="authorized" if fields else "refused",
            row=fields[0] if fields else "",
            uv=fields[UV_COLUMN] if fields else "",
            workroot=fields[WORKROOT_COLUMN] if fields else "",
            designated_hostnames=names,
            merged_table_text=table,
            decision_line=login_decision(table, host=request.host, login=request.login),
            message=message,
        )
