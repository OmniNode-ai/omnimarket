#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Route direct full-suite pytest runs through committed host authority (OMN-15977).

Narrow runs and CI are exempt. A missing private table designates no host;
untrusted tables refuse before any grant is consumed. Host mismatches require
a single-use, repo/HEAD-scoped grant shared with the shell hook (OMN-16480)."""

from __future__ import annotations

import argparse
import getpass
import importlib
import importlib.util
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

from scripts.hooks.prepush_override_grant import (
    GrantError,
    consume,
    env_rejection_message,
    has_active_override,
    inherited_override_env_vars,
    resolve_head_sha,
    resolve_repo_root,
)

_NODE_PACKAGE = "omnimarket.nodes.node_pytest_full_suite_host_guard"
_SRC = str(Path(__file__).resolve().parents[2] / "src")
_RESOLVER = Path(__file__).with_name("prepush_dispatch.sh")


def _load(module: str) -> Any:
    """A module of the host guard node (OMN-23367), loaded on first use.

    The shell hook runs this file with no project env, so the file finds the node through this
    checkout's own src and, when pydantic or yaml is missing, runs itself again under uv with them."""
    if (
        importlib.util.find_spec("pydantic") is None
        or importlib.util.find_spec("yaml") is None
    ) and __name__ == "__main__":
        with_ = [a for dep in ("pydantic", "pyyaml") for a in ("--with", dep)]
        again = ["uv", "run", "--no-project", *with_, "python", "-m"]
        os.execvp(
            "uv", [*again, "scripts.hooks.pytest_full_suite_host_guard", *sys.argv[1:]]
        )
    if _SRC not in sys.path:
        sys.path.insert(0, _SRC)
    return importlib.import_module(f"{_NODE_PACKAGE}.{module}")


def _node() -> Any:
    return _load("handlers.handler_pytest_full_suite_host_guard")


def _io() -> Any:
    return _load("adapters.host_table_reader")


HOST_TABLE_REL = "scripts/hooks/prepush_hosts.tsv"
# OMN-20939: the deployment's own rows live in a private table found through the
# workspace-config resolver, never in this repository.
PRIVATE_TABLE_REL = "config/onex/overlays/prepush_hosts/prepush_hosts.omnimarket.tsv"
# OMN-23355: a second login on a capacity host carries its own uv and run folder as a
# row of this overlay, found through the skill overlay roots; the ssh target stays
# a host and never names a login. The node (OMN-23367) reads the rows.
LOGIN_OVERLAY_NAME = "prepush_logins"
OVERLAY_ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"


def is_full_suite_target(**kwargs: Any) -> bool:
    return bool(_node().is_full_suite_target(**kwargs))


def login_decision(table_text: str, *, host: str, login: str) -> str:
    return str(_node().login_decision(table_text, host=host, login=login))


def full_suite_host_violation_message(**kwargs: Any) -> str | None:
    """The node's host rule, with the two table paths this script names."""
    return _node().full_suite_host_violation_message(
        private_table_rel=PRIVATE_TABLE_REL, host_table_rel=HOST_TABLE_REL, **kwargs
    )


def resolve_local_login() -> str:
    """The invoking account name, which ssh(1) also uses when no user is configured."""
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return ""


def _raw_table_text(
    repo_root: Path,
    env: dict[str, str] | os._Environ[str],
    private_rel: str = PRIVATE_TABLE_REL,
) -> str:
    """Shipped and private tables read atomically; divergent working bytes refuse."""
    shipped = _io().shipped_table_text(repo_root, HOST_TABLE_REL)
    return shipped + "\n" + (_private_table_text(env, private_rel) or "")


def _decide(
    table_text: str,
    env: dict[str, str] | os._Environ[str],
    *,
    host: str = "",
    login: str | None = None,
    private_rel: str = PRIVATE_TABLE_REL,
) -> Any:
    """The node's answer for a host and login over the table as read (OMN-23367)."""
    return _io().decide(
        table_text,
        env,
        host=host,
        login=resolve_local_login() if login is None else login,
        private_table_rel=private_rel,
        host_table_rel=HOST_TABLE_REL,
        overlay_name=LOGIN_OVERLAY_NAME,
        roots_env=OVERLAY_ROOTS_ENV,
    )


def _table_text(
    repo_root: Path,
    env: dict[str, str] | os._Environ[str],
    private_rel: str = PRIVATE_TABLE_REL,
    login: str | None = None,
) -> str:
    """The table with the invoking login's own uv and run folder in place of the host row's.

    ``login=""`` reads none (OMN-23355)."""
    return _decide(
        _raw_table_text(repo_root, env, private_rel),
        env,
        login=login,
        private_rel=private_rel,
    ).merged_table_text


def designated_hostnames(env: dict[str, str] | None = None) -> tuple[str, ...]:
    """Authorizing identities from the shared trusted table reader, with row overrides."""
    active_env: dict[str, str] | os._Environ[str] = os.environ if env is None else env
    return _decide(
        _raw_table_text(resolve_repo_root(), active_env), active_env
    ).designated_hostnames


def _workspace_config_root(env: dict[str, str] | os._Environ[str]) -> Path | None:
    """Private config root from injection or the workspace owner beside OMNI_HOME."""
    return _io().workspace_config_root(env, PRIVATE_TABLE_REL, _RESOLVER)


def _private_table_text(
    env: dict[str, str] | os._Environ[str], private_rel: str = PRIVATE_TABLE_REL
) -> str | None:
    """Read private bytes; absence is neutral, present untrusted state refuses."""
    return _io().private_table_text(_workspace_config_root(env), private_rel)


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
    # spawns this pytest. The node answers (OMN-23367).
    try:
        decision = _decide(
            _raw_table_text(resolve_repo_root(), os.environ), os.environ, host=host
        )
    except (ValueError, OSError, subprocess.SubprocessError, GrantError) as exc:
        pytest.exit(f"direct full-suite pytest invocation refused: {exc}", returncode=1)
    if decision.allowed:
        return
    # Only a host mismatch may spend a grant; invalid tables already refused.
    if override_authorization(
        context=f"direct full-suite pytest invocation on host '{host}'"
    ):
        return

    pytest.exit(decision.message or "", returncode=1)


def override_authorization(*, context: str) -> bool:
    """Honor an ancestor activation first, otherwise spend a scoped grant; fail closed."""
    try:
        repo_root = resolve_repo_root()
        head_sha = resolve_head_sha(repo_root)
        return (
            has_active_override(repo_root=repo_root, head_sha=head_sha)
            or consume(repo_root=repo_root, head_sha=head_sha, context=context).accepted
        )
    except Exception:  # noqa: BLE001 -- blanket by design: this runs inside
        # pytest_configure on EVERY invocation, so an unexpected exception must become
        # "not authorized", never a traceback that breaks collection for everyone.
        return False


def main() -> int:
    """The shell hook calls this same reader, including its refusal diagnostics."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--read-table", choices=("all", "private", "status", "decision"), required=True
    )
    parser.add_argument("--host", default="")
    parser.add_argument("--login", default=None)
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--private-table-rel", default=PRIVATE_TABLE_REL)
    args = parser.parse_args()
    result: str | None
    try:
        repo_root = args.repo_root or resolve_repo_root()
        rel = args.private_table_rel
        if args.read_table == "status":
            root = _workspace_config_root(os.environ)
            path = root / rel if root and rel else None
            result = _node().table_status_line(
                str(path) if path and path.exists() else None, rel, HOST_TABLE_REL
            )
        elif args.read_table == "private":
            result = _private_table_text(os.environ, rel)
        elif args.read_table == "decision":
            raw = _raw_table_text(repo_root, os.environ, rel)
            result = _decide(
                raw,
                os.environ,
                host=args.host or resolve_local_hostname(),
                login=args.login,
                private_rel=rel,
            ).decision_line
        else:
            result = _table_text(repo_root, os.environ, rel)
        if result is None:
            return 2
        sys.stdout.write(str(result))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError, GrantError) as exc:
        print(f"pre-push host table refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
