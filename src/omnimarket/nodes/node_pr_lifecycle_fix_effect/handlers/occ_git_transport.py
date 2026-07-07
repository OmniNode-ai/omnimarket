# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared OCC git transport helpers (HTTPS x-access-token) for the fix-effect adapters.

The .201 effects runtime container has **no SSH identity**, so both OCC companion
adapters (:class:`OccAutobindAdapter`, :class:`OccContractAdapter`) must clone and
push ``onex_change_control`` over HTTPS using an ``x-access-token`` credential
rather than an ``git@github.com:`` SSH remote (OMN-13990). The container already
resolves ``GITHUB_TOKEN`` from the contract-declared secret ref (OMN-12856), so
the only gap was the git transport.

The token is embedded in the clone/push URL (the same shape ``actions/checkout``
uses). :func:`run_git` scrubs any ``x-access-token:<secret>@`` credential from a
surfaced git error so the token never lands in a log line or exception message.
"""

from __future__ import annotations

import re
import subprocess

# Canonical OCC repo slug (owner/repo). The companion PRs land here.
OCC_REPO = "OmniNode-ai/onex_change_control"

# Matches the credential segment of an authenticated GitHub HTTPS URL so it can
# be redacted from any surfaced git stdout/stderr/command string. GitHub tokens
# (ghp_/gho_/github_pat_) never contain ``@``, so ``[^@\s]+`` is a safe capture.
_CREDENTIAL_URL_RE = re.compile(r"(x-access-token:)[^@\s]+(@)")


def authenticated_occ_url(token: str, repo: str = OCC_REPO) -> str:
    """Return an HTTPS clone/push URL carrying an ``x-access-token`` credential."""
    return f"https://x-access-token:{token}@github.com/{repo}.git"


def scrub_credentials(text: str) -> str:
    """Redact any ``x-access-token:<secret>@`` credential from ``text``."""
    if not text:
        return text
    return _CREDENTIAL_URL_RE.sub(r"\1***\2", text)


def _scrub_process_text(value: str | bytes | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    return scrub_credentials(value) or None


def run_git(argv: list[str], *, cwd: str, timeout: float = 300.0) -> str:
    """Run a git subprocess, returning stripped stdout.

    A ``timeout`` (default 300s) bounds network git operations (clone/push over
    HTTPS): without it a stalled remote or network partition would hang the
    calling thread indefinitely — and these run via ``asyncio.to_thread`` at the
    effect boundary (OMN-13990). On timeout a credential-scrubbed
    :class:`subprocess.TimeoutExpired` propagates.

    On failure the raised :class:`subprocess.CalledProcessError` is re-raised with
    its ``cmd``/``output``/``stderr`` credential-scrubbed, preserving the exception
    type (callers such as ``_head_sha`` rely on catching ``CalledProcessError``)
    while guaranteeing the embedded token never appears in a log or traceback.
    """
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        cmd_t = exc.cmd
        scrubbed_t: list[str] | str = (
            [scrub_credentials(str(part)) for part in cmd_t]
            if isinstance(cmd_t, (list, tuple))
            else scrub_credentials(str(cmd_t))
        )
        raise subprocess.TimeoutExpired(
            scrubbed_t,
            exc.timeout,
            output=_scrub_process_text(exc.output),
            stderr=_scrub_process_text(exc.stderr),
        ) from None
    except subprocess.CalledProcessError as exc:
        cmd = exc.cmd
        scrubbed_cmd: list[str] | str
        if isinstance(cmd, (list, tuple)):
            scrubbed_cmd = [scrub_credentials(str(part)) for part in cmd]
        else:
            scrubbed_cmd = scrub_credentials(str(cmd))
        raise subprocess.CalledProcessError(
            exc.returncode,
            scrubbed_cmd,
            output=scrub_credentials(exc.output or "") or None,
            stderr=scrub_credentials(exc.stderr or "") or None,
        ) from None
    return result.stdout.strip()


__all__ = [
    "OCC_REPO",
    "authenticated_occ_url",
    "run_git",
    "scrub_credentials",
]
