#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Route direct full-suite pytest runs through committed host authority (OMN-15977).

Narrow runs and CI are exempt. A missing private table designates no host;
untrusted tables refuse before any grant is consumed. Host mismatches require
a single-use, repo/HEAD-scoped grant shared with the shell hook (OMN-16480)."""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
from pathlib import Path

from scripts.hooks.prepush_override_grant import (
    GrantError,
    consume,
    env_rejection_message,
    has_active_override,
    inherited_override_env_vars,
    resolve_head_sha,
    resolve_repo_root,
)

HOST_TABLE_REL = "scripts/hooks/prepush_hosts.tsv"
# OMN-20939: the deployment's own rows live in a private table found through the
# workspace-config resolver, never in this repository.
PRIVATE_TABLE_REL = "config/onex/overlays/prepush_hosts/prepush_hosts.omnimarket.tsv"
_GIT_LOCATION_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)

# Legacy env override -> host-table row label. An override REPLACES the row it
# names; it never ADDS a hostname to the designated set. That distinction is
# load-bearing: under a table listing several hosts, an override that merely
# appended a name could no longer DE-designate the local machine, silently
# inverting the OMN-15059 guard that
# `test_guard_refuses_full_suite_escalation_on_non_200_host` proves by forcing
# a nonsense hostname.
_LEGACY_OVERRIDE_BY_LABEL = {
    "h200": "PREPUSH_200_HOSTNAME",
    "h201c": "PREPUSH_201_GATE_RUNNER_HOSTNAME",
}


def _override_var(label: str) -> str:
    """Per-row override env var name, matching prepush_dispatch.sh."""
    sanitized = "".join(c if c.isalnum() else "_" for c in label.upper())
    return f"PREPUSH_HOST_OVERRIDE_{sanitized}"


def designated_hostnames(
    env: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Authorizing identities from the shared trusted table reader, with row overrides."""
    active_env: dict[str, str] | os._Environ[str] = os.environ if env is None else env
    table_text = _table_text(resolve_repo_root(), active_env)
    names: list[str] = []
    for line in table_text.splitlines():
        fields = line.split("#", 1)[0].split("\t")
        if len(fields) < 12 or fields[11] != "authorizing":
            continue
        label, hostname = fields[0], fields[2]
        legacy = _LEGACY_OVERRIDE_BY_LABEL.get(label)
        if legacy and active_env.get(legacy):
            hostname = active_env[legacy]
        names.append(active_env.get(_override_var(label)) or hostname)
    return tuple(names)


def _table_text(
    repo_root: Path,
    env: dict[str, str] | os._Environ[str],
    private_rel: str = PRIVATE_TABLE_REL,
) -> str:
    """Read shipped and private tables atomically, refusing divergent working bytes."""
    committed = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"HEAD:{HOST_TABLE_REL}"],
        capture_output=True,
        timeout=15,
        check=False,
    )
    if committed.returncode != 0:
        raise ValueError(f"host table absent at HEAD ({HOST_TABLE_REL})")
    working = repo_root / HOST_TABLE_REL
    if working.exists() and working.read_bytes() != committed.stdout:
        raise ValueError("host table differs between the working tree and HEAD")
    private = _private_table_text(env, private_rel)
    return committed.stdout.decode("utf-8") + "\n" + (private or "")


def _workspace_config_root(
    env: dict[str, str] | os._Environ[str],
) -> Path | None:
    """Private config root from injection or the workspace owner beside OMNI_HOME."""
    configured = env.get("ONEX_WORKSPACE_CONFIG_ROOT", "").strip()
    if configured:
        return Path(configured)
    if not env.get("OMNI_HOME", "").strip():
        return None
    resolved = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; prepush_workspace_config_root',
            "resolver",
            str(Path(__file__).with_name("prepush_dispatch.sh")),
        ],
        env={**env, "PREPUSH_PRIVATE_TABLE_REL": PRIVATE_TABLE_REL},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return (
        Path(resolved.stdout.strip())
        if resolved.returncode == 0 and resolved.stdout.strip()
        else None
    )


def _private_table_text(
    env: dict[str, str] | os._Environ[str], private_rel: str = PRIVATE_TABLE_REL
) -> str | None:
    """Read private bytes; absence is neutral, present untrusted state refuses."""
    root = _workspace_config_root(env)
    if root is None or not private_rel:
        return None
    path = root / private_rel
    try:
        working = path.read_bytes()
    except FileNotFoundError:
        if not path.is_symlink():
            return None
        raise ValueError(
            "private host table cannot be read: dangling symlink"
        ) from None
    except OSError as exc:
        raise ValueError(f"private host table cannot be read: {exc.strerror}") from exc
    git_env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_ENV}
    top = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        env=git_env,
    )
    if top.returncode == 0 and Path(top.stdout.strip()).resolve() == root.resolve():
        committed = subprocess.run(
            ["git", "-C", str(root), "show", f"HEAD:{private_rel}"],
            capture_output=True,
            timeout=15,
            check=False,
            env=git_env,
        )
        if committed.returncode != 0:
            raise ValueError("private host table has no committed copy at HEAD")
        if working != committed.stdout:
            raise ValueError(
                "private host table differs between the working tree and HEAD"
            )
    return working.decode("utf-8")


def is_ci_environment(env: dict[str, str] | None = None) -> bool:
    """True when running under a CI runner -- this guard never gates CI."""
    active_env: dict[str, str] | os._Environ[str] = os.environ if env is None else env
    return bool(active_env.get("CI") or active_env.get("GITHUB_ACTIONS"))


def resolve_local_hostname() -> str:
    """Short hostname, or an empty string when identity cannot be determined."""
    try:
        return socket.gethostname().split(".", 1)[0]
    except OSError:
        return ""


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
    targets = args or testpaths
    normalized_full = full_suite_target.rstrip("/") + "/"
    for raw in targets:
        normalized = str(raw).rstrip("/") + "/"
        if normalized_full.startswith(normalized):
            return True
    return False


def full_suite_host_violation_message(
    *,
    host: str,
    target_hostname: str,
    additional_target_hostnames: tuple[str, ...] = (),
    override_authorized: bool,
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
        f"${{ONEX_WORKSPACE_CONFIG_ROOT}}/{PRIVATE_TABLE_REL} and set "
        "ONEX_WORKSPACE_CONFIG_ROOT to its root, then run from a configured gate host "
        f"(schema and procedure: {HOST_TABLE_REL}). OR mint a single-use override "
        "grant to run here anyway (visible, receipted, degraded-evidence -- do not "
        "use as a routine bypass): `uv run python scripts/hooks/prepush_override_grant.py "
        "mint --reason '<why>'`."
    )


def override_authorization(*, context: str) -> bool:
    """Honor an ancestor activation first, otherwise spend a scoped grant; fail closed."""
    try:
        repo_root = resolve_repo_root()
        head_sha = resolve_head_sha(repo_root)
    except Exception:  # noqa: BLE001 -- see fail-closed note below
        # Blanket by design, both here and below. This runs inside
        # pytest_configure on EVERY invocation: an unexpected exception must
        # become "not authorized", never a traceback that breaks collection for
        # everyone. Narrowing the clause would trade a fail-closed refusal for a
        # crash in the one path that must not crash.
        return False
    try:
        if has_active_override(repo_root=repo_root, head_sha=head_sha):
            return True
        return consume(repo_root=repo_root, head_sha=head_sha, context=context).accepted
    except Exception:  # noqa: BLE001 -- fail closed, never crash collection
        return False


def enforce(config: object, full_suite_target: str) -> None:
    """Refuse before collection; targeted runs remain runnable without host authority."""
    if is_ci_environment():
        return
    option = config.option  # type: ignore[attr-defined]
    if not is_full_suite_target(
        args=list(config.args),  # type: ignore[attr-defined]
        testpaths=list(config.getini("testpaths") or []),  # type: ignore[attr-defined]
        keyword=option.keyword or "",
        markexpr=option.markexpr or "",
        full_suite_target=full_suite_target,
    ):
        return

    import pytest

    # OMN-16480: a leaked PREPUSH_ALLOW_* variable is a REFUSAL, not a bypass.
    # Checked after the narrowing test (a targeted run is never gated) but
    # before the host check, so the leak is reported on the host where it
    # actually causes damage -- and so the OMN-16425 recursion terminates here
    # instead of spawning another full suite.
    leaked = inherited_override_env_vars(os.environ)
    if leaked:
        pytest.exit(env_rejection_message(leaked), returncode=1)

    host = resolve_local_hostname()
    # OMN-16991: identity comes from the committed host table, not from
    # PREPUSH_* env vars the bash hook's scrub deliberately strips before it
    # spawns this pytest. See designated_hostnames().
    try:
        all_designated = designated_hostnames()
    except (ValueError, OSError, subprocess.SubprocessError, GrantError) as exc:
        pytest.exit(f"direct full-suite pytest invocation refused: {exc}", returncode=1)
    target_hostname = all_designated[0] if all_designated else ""
    message = full_suite_host_violation_message(
        host=host,
        target_hostname=target_hostname,
        additional_target_hostnames=all_designated[1:],
        override_authorized=False,
    )
    if message is None:
        return
    # Only a host mismatch may spend a grant; invalid tables already refused.
    if override_authorization(
        context=f"direct full-suite pytest invocation on host '{host}'"
    ):
        return

    pytest.exit(message, returncode=1)


def main() -> int:
    """The shell hook calls this same reader, including its refusal diagnostics."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--read-table", choices=("all", "private", "status"), required=True
    )
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--private-table-rel", default=PRIVATE_TABLE_REL)
    args = parser.parse_args()
    result: str | None
    try:
        if args.read_table == "status":
            root = _workspace_config_root(os.environ)
            path = (
                root / args.private_table_rel
                if root and args.private_table_rel
                else None
            )
            result = (
                f"deployment host table: {path}"
                if path and path.exists()
                else f"deployment host table NOT CONFIGURED: {args.private_table_rel} is absent -- set ONEX_WORKSPACE_CONFIG_ROOT "
                f"to a root holding {args.private_table_rel} (fifteen columns, see {HOST_TABLE_REL})"
            )
        elif args.read_table == "private":
            result = _private_table_text(os.environ, args.private_table_rel)
        else:
            result = _table_text(
                args.repo_root or resolve_repo_root(),
                os.environ,
                args.private_table_rel,
            )
        if result is None:
            return 2
        sys.stdout.write(str(result))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError, GrantError) as exc:
        print(f"pre-push host table refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
