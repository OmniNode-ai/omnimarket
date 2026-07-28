# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared OCC git transport helpers (HTTPS x-access-token).

Promoted to a shared top-level module (OMN-14622) so every OCC companion
producer — the legacy :class:`OccCompanionEmitter`, its read-back verifier, and
the deterministic ``node_occ_companion_effect`` (RSD-3) — clones/pushes
``onex_change_control`` through ONE transport instead of a per-node copy
(net-negative-surface).

The .201 effects runtime container has **no SSH identity**, so OCC companion
producers must clone and push ``onex_change_control`` over HTTPS using an
``x-access-token`` credential rather than a ``git@github.com:`` SSH remote
(OMN-13990). The container already resolves ``GITHUB_TOKEN`` from the
contract-declared secret ref (OMN-12856), so the only gap was the git transport.

The token is embedded in the clone/push URL (the same shape ``actions/checkout``
uses). :func:`run_git` scrubs any ``x-access-token:<secret>@`` credential from a
surfaced git error so the token never lands in a log line or exception message.
"""

from __future__ import annotations

import json
import logging
import random
import re
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from omnimarket.github_api import (
    GitHubApiError,
    rest_json,
    rest_json_array,
    rest_no_content,
    split_repo,
)

logger = logging.getLogger(__name__)

# OMN-15347: bounded retry for transient GitHub transport failures observed
# live 2026-07-28 (RemoteDisconnected inside _resolve_reusable_tree_sha, zero
# retry anywhere in the acquire chain). 3 attempts total, short exponential
# backoff with jitter — retries only network/5xx transport shapes, never a 422
# (contention is a semantic outcome, not a transport error) or any other 4xx.
_RETRY_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 0.2

# Canonical OCC repo slug (owner/repo). The companion PRs land here.
OCC_REPO = "OmniNode-ai/onex_change_control"

# OMN-14793 (OMN-14783 rec #2): single-producer lease ref namespace. Both live
# OccCompanionEmitter instances (the local merge_sweep mint path and the .201
# effects lane) run on different hosts with no shared in-process state and write
# the SAME deterministic ``auto/…-occ-autobind`` branch which they force-push, so
# "branch exists" is not a discriminator. The only durable surface both producers
# share is the OCC git repo itself, so the lease is an atomic create-if-absent on
# a git-ref there — first-acquirer-wins keyed on PR head SHA, regardless of host.
_OCC_LEASE_REF_PREFIX = "refs/occ-companion-leases/"

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


def _is_transient_github_error(exc: GitHubApiError) -> bool:
    """True for transport shapes worth retrying.

    ``status_code is None`` covers the network/decode branch of
    :func:`omnimarket.github_api.rest_json` (``urllib.error.URLError`` /
    ``OSError`` — e.g. the ``RemoteDisconnected`` seen live in OMN-15347); a 5xx
    is a transient server-side failure. A 422 (contention — the ref/commit
    already exists, a normal outcome of the lease protocol) and every other 4xx
    (client error — retrying cannot help) are never retried.
    """
    return exc.status_code is None or exc.status_code >= 500


def _call_with_retry[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Call ``fn(*args, **kwargs)``, retrying transient :class:`GitHubApiError`.

    Bounded at :data:`_RETRY_MAX_ATTEMPTS` attempts total with short exponential
    backoff plus jitter between attempts. Only errors classified transient by
    :func:`_is_transient_github_error` are retried — a 422 or any other
    non-transient error propagates unchanged on the first attempt, preserving
    the module's existing fail-closed posture. After the attempt cap is
    exhausted the last exception is re-raised exactly as raised today (never
    swallowed).
    """
    for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
        try:
            return fn(*args, **kwargs)
        except GitHubApiError as exc:
            if not _is_transient_github_error(exc) or attempt == _RETRY_MAX_ATTEMPTS:
                raise
            delay = _RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
            delay += random.uniform(0, delay * 0.5)
            logger.warning(
                "occ_companion_lease: transient GitHub API error on attempt "
                "%d/%d: %s (retrying in %.2fs)",
                attempt,
                _RETRY_MAX_ATTEMPTS,
                exc,
                delay,
            )
            time.sleep(delay)
    raise AssertionError("unreachable: loop always returns or raises")


# ---------------------------------------------------------------------------
# Single-producer lease (OMN-14793 / OMN-14783 rec #2)
# ---------------------------------------------------------------------------


def _lease_key(repo_slug: str, pr_number: int, head_sha: str) -> str:
    """Return the lease key for a product PR head.

    Keyed on the product PR **head SHA** (not the branch, not the producer
    identity): both producers observe the identical ``head.sha`` from GitHub and
    commit as the same ``omnimarket-bot`` identity rendering deterministic
    content, so head SHA is the only correct discriminator. Normalised to the
    ``<owner>-<repo>-pr-<n>-<head>`` shape (slashes → dashes, lower-cased) so the
    key is stable whether the caller passes ``owner/repo`` or ``owner-repo``.
    """
    normalized = repo_slug.replace("/", "-").lower()
    return f"{normalized}-pr-{pr_number}-{head_sha}"


def _resolve_reusable_tree_sha(owner: str, repo_name: str, token: str) -> str:
    """Return an existing tree SHA to point a lease commit at.

    ``POST /repos/{owner}/{repo}/git/trees`` unconditionally rejects an empty
    ``tree: []`` array with ``422 "Invalid tree info"`` on this GitHub API
    surface — reproduced live 2026-07-23 (OMN-14981): a bare empty array, an
    empty array with ``base_tree`` set, and ``base_tree`` alone all 422
    identically, so there is no request shape that mints a *new* empty tree
    object via this endpoint. A lease commit does not need a **new** tree —
    only *some* existing, already-committed tree to point at: the commit is
    parentless, lives only under ``refs/occ-companion-leases/…``, is never
    merged, and no consumer ever reads its tree contents — so reuse the
    repo's current default-branch tree instead of trying to create one.
    """
    repo_info = _call_with_retry(
        rest_json, "GET", f"/repos/{owner}/{repo_name}", token=token
    )
    default_branch = repo_info.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise GitHubApiError(
            f"repo {owner}/{repo_name} has no default_branch: {repo_info!r}"
        )
    head_commit = _call_with_retry(
        rest_json,
        "GET",
        f"/repos/{owner}/{repo_name}/commits/{default_branch}",
        token=token,
    )
    commit_info = head_commit.get("commit")
    tree = commit_info.get("tree") if isinstance(commit_info, dict) else None
    tree_sha = tree.get("sha") if isinstance(tree, dict) else None
    if not isinstance(tree_sha, str):
        raise GitHubApiError(
            f"default branch {default_branch} commit has no tree sha: {head_commit!r}"
        )
    return tree_sha


def _create_lease_commit(
    owner: str,
    repo_name: str,
    token: str,
    *,
    producer_id: str,
    pr_number: int,
    head_sha: str,
) -> str:
    """Create a fresh lease commit and return its SHA.

    A parentless commit pointing at a reused, pre-existing tree (see
    :func:`_resolve_reusable_tree_sha`), so it materialises no NEW files and
    can never collide with real OCC content. GitHub sets ``committer.date`` to
    the current server time when it is omitted, giving a *server-authoritative*
    acquisition clock — read back on a 422 to decide TTL expiry without depending
    on either producer host's wall clock (skew-free, OMN-14783 build note §6.2).
    The lease metadata is also encoded in the commit message for forensics.
    """
    tree_sha = _resolve_reusable_tree_sha(owner, repo_name, token)
    message = json.dumps(
        {
            "occ_companion_lease": True,
            "producer_id": producer_id,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "acquired_at": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        sort_keys=True,
    )
    commit = _call_with_retry(
        rest_json,
        "POST",
        f"/repos/{owner}/{repo_name}/git/commits",
        token=token,
        body={"message": message, "tree": tree_sha, "parents": []},
    )
    commit_sha = commit.get("sha")
    if not isinstance(commit_sha, str):
        raise GitHubApiError(f"lease commit create returned no sha: {commit!r}")
    return commit_sha


def _lease_is_stale(
    owner: str,
    repo_name: str,
    ref_short: str,
    token: str,
    lease_ttl_seconds: int,
) -> bool:
    """True when the existing lease commit is older than the TTL (stealable).

    Reads the ref's target commit ``committer.date`` — the server-authoritative
    acquisition time — and compares it to now. A ref that vanished between our
    failed create and this read (404) is treated as stealable. Any other transport
    error propagates so acquisition fails CLOSED rather than silently stealing.
    """
    try:
        ref = rest_json(
            "GET",
            f"/repos/{owner}/{repo_name}/git/ref/{ref_short}",
            token=token,
        )
    except GitHubApiError as exc:
        if exc.status_code == 404:
            return True
        raise
    obj = ref.get("object")
    sha = obj.get("sha") if isinstance(obj, dict) else None
    if not isinstance(sha, str):
        raise GitHubApiError(f"lease ref {ref_short} has no object sha: {ref!r}")
    commit = rest_json(
        "GET",
        f"/repos/{owner}/{repo_name}/git/commits/{sha}",
        token=token,
    )
    committer = commit.get("committer")
    date_str = committer.get("date") if isinstance(committer, dict) else None
    if not isinstance(date_str, str):
        raise GitHubApiError(f"lease commit {sha} has no committer date: {commit!r}")
    acquired_at = datetime.fromisoformat(date_str)
    age_seconds = (datetime.now(tz=UTC) - acquired_at).total_seconds()
    return age_seconds > lease_ttl_seconds


def _create_lease_ref(
    owner: str,
    repo_name: str,
    ref_full: str,
    lease_sha: str,
    token: str,
) -> bool:
    """Atomic create-if-absent of the lease ref. True=201 created, False=422 exists.

    ``POST /git/refs`` is GitHub's guaranteed server-side atomic create: it returns
    201 on create and 422 "Reference already exists" if the ref is present. Any
    other status propagates (fail-closed).
    """
    try:
        _call_with_retry(
            rest_json,
            "POST",
            f"/repos/{owner}/{repo_name}/git/refs",
            token=token,
            body={"ref": ref_full, "sha": lease_sha},
        )
        return True
    except GitHubApiError as exc:
        if exc.status_code == 422:
            return False
        raise


_OCC_LEASE_REF_NAMESPACE = _OCC_LEASE_REF_PREFIX.removeprefix("refs/")


def _reap_stale_sibling_leases(
    owner: str,
    repo_name: str,
    repo_slug: str,
    pr_number: int,
    head_sha: str,
    token: str,
    lease_ttl_seconds: int,
) -> None:
    """Opportunistically GC expired same-PR, different-head lease refs.

    OMN-15347: a lease is keyed on ``(repo, pr_number, head_sha)``. When a PR
    receives new commits (or is force-pushed) after a lease was acquired for its
    prior head, that old ``(repo, pr, old_head)`` key is never revisited by any
    future acquire — the existing TTL-steal path only fires on a *same-key* 422
    contention, so the stale ref becomes permanent debris (the live orphan:
    ``omnibase_core#1494``, head moved past the leased SHA). This lists sibling
    lease refs under the same repo+PR prefix and deletes any whose head_sha
    differs from ``head_sha`` (this acquisition's target) AND whose TTL has
    expired, giving that orphan class an actual deletion path.

    Scope discipline: never touches another PR's refs (prefix-filtered), never
    touches an unexpired sibling, and never touches the current-head key (that
    ref only ever moves through the existing steal-on-contention logic below).

    Best-effort by contract: any failure — listing, staleness read, or delete —
    is logged and swallowed. This runs opportunistically alongside a real
    acquire attempt and must never fail (or slow down, beyond one extra list
    call) that acquire.
    """
    normalized = repo_slug.replace("/", "-").lower()
    pr_prefix = f"{normalized}-pr-{pr_number}-"
    try:
        matches = rest_json_array(
            "GET",
            f"/repos/{owner}/{repo_name}/git/matching-refs/"
            f"{_OCC_LEASE_REF_NAMESPACE}{pr_prefix}",
            token=token,
        )
    except GitHubApiError as exc:
        if exc.status_code == 404:
            matches = []
        else:
            logger.warning(
                "occ_companion_lease: sibling-reap listing failed for pr=%s: %s",
                pr_number,
                exc,
            )
            return
    except OSError as exc:  # fallback-ok: reap must never fail the real acquire
        logger.warning(
            "occ_companion_lease: sibling-reap listing errored for pr=%s: %s",
            pr_number,
            exc,
        )
        return

    for match in matches:
        ref_name = match.get("ref") if isinstance(match, dict) else None
        if not isinstance(ref_name, str) or not ref_name.startswith(
            _OCC_LEASE_REF_PREFIX
        ):
            continue
        key = ref_name[len(_OCC_LEASE_REF_PREFIX) :]
        if not key.startswith(pr_prefix):
            continue  # defensive: matching-refs can prefix-match beyond our PR
        sibling_head = key[len(pr_prefix) :]
        if sibling_head == head_sha:
            continue  # current-head key — only ever moves via steal-on-contention
        ref_short = ref_name.removeprefix("refs/")
        try:
            if not _lease_is_stale(
                owner, repo_name, ref_short, token, lease_ttl_seconds
            ):
                continue  # unexpired sibling — leave it alone
            rest_no_content(
                "DELETE",
                f"/repos/{owner}/{repo_name}/git/refs/{ref_short}",
                token=token,
            )
        except GitHubApiError as exc:
            logger.warning(
                "occ_companion_lease: best-effort sibling reap of %s failed: %s",
                key,
                exc,
            )
        except OSError as exc:  # fallback-ok: reap must never fail the real acquire
            logger.warning(
                "occ_companion_lease: best-effort sibling reap of %s errored: %s",
                key,
                exc,
            )


def acquire_occ_companion_lease(
    *,
    token: str,
    repo_slug: str,
    pr_number: int,
    head_sha: str,
    producer_id: str,
    lease_ttl_seconds: int,
    occ_repo: str = OCC_REPO,
) -> bool:
    """Atomically acquire the single-producer lease for a product PR head.

    Returns ``True`` when this producer won the lease (it MUST proceed to mint and
    MUST later call :func:`release_occ_companion_lease`), ``False`` when a live
    producer already holds it (this producer MUST no-op with zero side effects).

    Mechanism: create a fresh lease commit (server-authoritative timestamp) and
    atomically create ``refs/occ-companion-leases/<repo>-pr-<pr>-<head>`` pointing
    at it. On 422 (already held) the existing lease is stolen only if its commit
    is older than ``lease_ttl_seconds`` (a crashed producer that never released) —
    otherwise a live producer holds it and this returns ``False``.

    Fail-closed: any transport error other than the expected 201/422 propagates,
    so an unreachable lease surface can never silently re-enable dual authoring.
    """
    owner, repo_name = split_repo(occ_repo)
    key = _lease_key(repo_slug, pr_number, head_sha)
    ref_full = f"{_OCC_LEASE_REF_PREFIX}{key}"
    ref_short = f"occ-companion-leases/{key}"

    # OMN-15347: opportunistic GC for expired same-PR, different-head lease
    # refs. Runs before the real acquire attempt. _reap_stale_sibling_leases is
    # itself best-effort (swallows every GitHubApiError/OSError internally) —
    # this call-site firewall is defense in depth so a real acquire can never
    # be taken down by an unanticipated bug in the opportunistic GC path.
    try:
        _reap_stale_sibling_leases(
            owner, repo_name, repo_slug, pr_number, head_sha, token, lease_ttl_seconds
        )
    except Exception as exc:
        logger.warning(
            "occ_companion_lease: sibling-reap for pr=%s raised unexpectedly: %s",
            pr_number,
            exc,
        )

    lease_sha = _create_lease_commit(
        owner,
        repo_name,
        token,
        producer_id=producer_id,
        pr_number=pr_number,
        head_sha=head_sha,
    )

    if _create_lease_ref(owner, repo_name, ref_full, lease_sha, token):
        return True

    # Ref already exists — a live producer holds it, unless it is stale (crashed).
    if not _lease_is_stale(owner, repo_name, ref_short, token, lease_ttl_seconds):
        return False

    # Steal the stale lease: delete it, then re-create pointing at our fresh
    # commit. Deletion may 404/422 if another producer already stole it — that is
    # fine, we still try the create and lose the race gracefully if it 422s.
    try:
        rest_no_content(
            "DELETE",
            f"/repos/{owner}/{repo_name}/git/refs/{ref_short}",
            token=token,
        )
    except GitHubApiError as exc:
        if exc.status_code not in (404, 422):
            raise
    return _create_lease_ref(owner, repo_name, ref_full, lease_sha, token)


def release_occ_companion_lease(
    *,
    token: str,
    repo_slug: str,
    pr_number: int,
    head_sha: str,
    occ_repo: str = OCC_REPO,
) -> None:
    """Release the single-producer lease (best-effort).

    Called from the mint's ``finally`` on BOTH success and failure, so a crashed
    or failed mint frees the head immediately instead of wedging the next
    legitimate attempt until the TTL steal fires. Best-effort by contract: a
    missing ref (404/422) is expected; any other error is logged and swallowed so
    releasing can never mask the mint's real outcome (it runs inside ``finally``).
    """
    owner, repo_name = split_repo(occ_repo)
    key = _lease_key(repo_slug, pr_number, head_sha)
    try:
        _call_with_retry(
            rest_no_content,
            "DELETE",
            f"/repos/{owner}/{repo_name}/git/refs/occ-companion-leases/{key}",
            token=token,
        )
    except GitHubApiError as exc:
        if exc.status_code in (404, 422):
            return
        logger.warning(
            "occ_companion_lease: best-effort release of %s failed: %s", key, exc
        )
    except OSError as exc:  # fallback-ok: release must never mask the mint outcome
        logger.warning(
            "occ_companion_lease: best-effort release of %s errored: %s", key, exc
        )


__all__ = [
    "OCC_REPO",
    "acquire_occ_companion_lease",
    "authenticated_occ_url",
    "release_occ_companion_lease",
    "run_git",
    "scrub_credentials",
]
