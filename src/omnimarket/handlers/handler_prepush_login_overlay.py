# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-login uv and run folder rows for the pre-push host table (OMN-23355).

A second login on a capacity host carries its own uv and run folder as a row of the
``prepush_logins`` overlay, found through the skill overlay roots. The reader replaces
``uv_abs_path`` and ``workroot`` of the host row for the invoking login only; the ssh
target stays a host and never names a login. Standard library only: the pre-push
guard imports this module without the project environment."""

from __future__ import annotations

import getpass
import os
import re
from collections.abc import Mapping
from pathlib import Path

LOGIN_OVERLAY_NAME = "prepush_logins"
OVERLAY_ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"
_UV_COLUMN = 5
_WORKROOT_COLUMN = 7


def resolve_local_login() -> str:
    """The invoking account name, which ssh(1) also uses when no user is configured."""
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return ""


def _login_overlay_block(env: Mapping[str, str]) -> str | None:
    """The ``table`` block of the first per-login overlay on the roots; absence is neutral."""
    for root in env.get(OVERLAY_ROOTS_ENV, "").split(os.pathsep):
        if not root:
            continue
        path = Path(root) / LOGIN_OVERLAY_NAME / "overlay.yaml"
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"per-login host overlay cannot be read: {exc}") from exc
        lines = text.splitlines()
        start = next(
            (
                i
                for i, ln in enumerate(lines)
                if re.fullmatch(r"table:\s*\|[-+]?\s*", ln)
            ),
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
    return None


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
        if len(fields) >= 15 and not line.lstrip().startswith("#"):
            owned = rows.get((fields[0], login))
            if owned:
                fields[_UV_COLUMN], fields[_WORKROOT_COLUMN] = owned
                line = "\t".join(fields)
        out.append(line)
    return "\n".join(out)


def login_decision(table_text: str, *, host: str, login: str) -> str:
    """One line naming the guard's decision for a login on a host and what it resolves to."""
    for line in table_text.splitlines():
        fields = line.split("#", 1)[0].split("\t")
        if len(fields) < 15 or fields[2].lower() != host.lower():
            continue
        if fields[11] == "authorizing":
            return (
                f"prepush-guard decision: host={host} login={login} row={fields[0]} "
                f"verdict=authorized uv={fields[_UV_COLUMN]} "
                f"workroot={fields[_WORKROOT_COLUMN]}"
            )
    return (
        f"prepush-guard decision: host={host} login={login} verdict=refused "
        "reason=not-designated"
    )


def apply_login_overlay(
    table_text: str, env: Mapping[str, str], login: str | None
) -> str:
    """The table text with the invoking login's rows applied; no overlay changes nothing."""
    block = _login_overlay_block(env)
    if block is None:
        return table_text
    who = resolve_local_login() if login is None else login
    return apply_login_rows(table_text, parse_login_rows(block), who)


def _override_var(label: str) -> str:
    """Per-row override env var name, matching prepush_dispatch.sh."""
    sanitized = "".join(c if c.isalnum() else "_" for c in label.upper())
    return f"PREPUSH_HOST_OVERRIDE_{sanitized}"


def is_ci_environment(env: Mapping[str, str] | None = None) -> bool:
    """True when running under a CI runner -- this guard never gates CI."""
    active_env: Mapping[str, str] = os.environ if env is None else env
    return bool(active_env.get("CI") or active_env.get("GITHUB_ACTIONS"))


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
