# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Git boundary for the ticket-contract prerequisite, repairing absent remote bases."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import yaml

from omnimarket.nodes.contract_topics import contract_section
from omnimarket.nodes.node_dod_verify.services.contract_prerequisite import (
    cited_tickets,
    contract_prerequisite,
)
from omnimarket.nodes.node_ticket_contract_prerequisite_effect.handlers.handler_integration_proof import (
    check_integration_proof,
)
from omnimarket.nodes.node_ticket_contract_prerequisite_effect.models.model_integration_proof_request import (
    ModelIntegrationProofRequest,
)
from omnimarket.nodes.node_ticket_contract_prerequisite_effect.models.model_ticket_contract_prerequisite_request import (
    ModelTicketContractPrerequisiteRequest,
)
from omnimarket.nodes.node_ticket_contract_prerequisite_effect.models.model_ticket_contract_prerequisite_result import (
    ModelTicketContractPrerequisiteResult,
)
from omnimarket.nodes.node_umbrella_ticket_compute.handlers.handler_umbrella_ticket import (
    PR_FIX,
    umbrella_evidence,
)

_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
_UMBRELLA_ENV = "ONEX_UMBRELLA_TICKETS"
_BODY_ENV = "PR_BODY"
_ENTRY_SECTIONS = ("requirements", "dod_evidence")


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, timeout=60
    ).stdout.strip()


def _resolve_base(base: str) -> str | None:
    """Pin the base, fetching only an absent configured remote-tracking branch.

    A single-branch worker clone need not have origin/dev, even when its objects
    are present. An explicit destination refspec repairs that clone regardless
    of its configured fetch refspec. Local refs and object ids are never guessed
    into remote branches. Fetch errors propagate to the failed typed verdict. A
    base that is unreadable and names no remote returns None, which callers treat
    as an unreadable diff (the refusal stands).
    """
    try:
        return _git("rev-parse", "--verify", f"{base}^{{commit}}")
    except subprocess.CalledProcessError:
        tracking = base.removeprefix("refs/remotes/")
        remotes = sorted(_git("remote").splitlines(), key=len, reverse=True)
        remote = next((r for r in remotes if tracking.startswith(f"{r}/")), None)
        if remote is None:
            return None
        branch = tracking[len(remote) + 1 :]
        _git("check-ref-format", f"refs/heads/{branch}")
        _git(
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "--no-write-fetch-head",
            "--",
            remote,
            f"refs/heads/{branch}:refs/remotes/{remote}/{branch}",
        )
        return _git("rev-parse", "--verify", f"{base}^{{commit}}")


# The receipt gate admits a release cut only while its diff against the merge base
# stays inside these root paths, so a cut cannot carry a contracts/ file.
_RELEASE_CUT_PATHS = frozenset({"CHANGELOG.md", "pyproject.toml", "uv.lock"})


def _is_release_cut(sha: str, base: str | None) -> bool:
    """Whether the diff against the merge base is a root changelog plus optional pins."""
    try:
        base_sha = _resolve_base(base) if base is not None else None
        if base_sha is None:
            return False
        merge_base = _git("merge-base", base_sha, sha)
        listing = _git("diff", "--name-only", "--no-renames", "-z", merge_base, sha)
    except (OSError, subprocess.SubprocessError):
        return False
    paths = {path for path in listing.split("\0") if path}
    return "CHANGELOG.md" in paths and paths <= _RELEASE_CUT_PATHS


# GitHub alone commits a squash merge to dev, as this committer, and names the
# pull request at the end of the subject.
_GITHUB_COMMITTER = "noreply@github.com"
_SQUASH_MERGED_SUBJECT = re.compile(r" \(#[0-9]+\)$")


def _own_revisions(sha: str, base_sha: str) -> tuple[str, ...]:
    """Revision arguments that select the commits this push owns.

    A clone's ``origin/dev`` can sit hours behind the branch it pushes, and then
    every pull request that landed in between lies in ``base..sha``. Those are
    the squash merges GitHub made on dev; they, and everything beneath them, are
    dev's and are hidden. A commit a lane wrote carries the lane's own committer,
    so a subject that merely ends ``(#N)`` hides nothing.
    """
    listing = _git(
        "log", "--no-merges", "--format=%H%x09%ce%x09%s", f"{base_sha}..{sha}"
    )
    merged: list[str] = []
    for line in listing.splitlines():
        commit, _, rest = line.partition("\t")
        committer, _, subject = rest.partition("\t")
        if committer == _GITHUB_COMMITTER and _SQUASH_MERGED_SUBJECT.search(subject):
            merged.append(commit)
    return (sha, "--not", base_sha, *merged)


def _changed_paths(sha: str, base: str | None) -> tuple[str, ...]:
    """The paths this push's own commits change; unreadable fails closed.

    A merge commit contributes only what its resolution added beyond both
    parents (``--cc``), so a clean merge of dev adds nothing and a conflict
    resolved into new code still counts.
    """
    try:
        base_sha = _resolve_base(base) if base is not None else None
        if base_sha is None:
            return ("src/<unreadable diff>",)
        listing = _git(
            "log",
            "--cc",
            "--no-renames",
            "--format=",
            "--name-only",
            "-z",
            *_own_revisions(sha, base_sha),
        )
    except (OSError, subprocess.SubprocessError):
        # An unreadable diff cannot show that nothing runs, so it counts as a
        # change that runs (unknown fails closed as runtime).
        return ("src/<unreadable diff>",)
    return tuple(dict.fromkeys(path for path in listing.split("\0") if path))


def umbrella_ticket_set() -> tuple[str, ...]:
    """The umbrella tickets: the deployment variable when set, else the contract's list."""
    declared = os.environ.get(_UMBRELLA_ENV, "").strip()
    if declared:
        return tuple(ticket.strip() for ticket in declared.split(",") if ticket.strip())
    listed = contract_section(_CONTRACT_PATH, "config", "umbrella_tickets")
    if not isinstance(listed, list) or not listed:
        raise ValueError(f"{_CONTRACT_PATH}: config.umbrella_tickets is empty")
    return tuple(str(ticket) for ticket in listed)


def _entry_ids(text: str) -> set[str]:
    loaded = yaml.safe_load(text) if text else None
    ids: set[str] = set()
    if not isinstance(loaded, dict):
        return ids
    for section in _ENTRY_SECTIONS:
        entries = loaded.get(section)
        for entry in entries if isinstance(entries, list) else ():
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                ids.add(entry["id"])
    return ids


def _blob(sha: str, path: str) -> str:
    """A file at a commit; empty when the commit does not hold it."""
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{sha}:{path}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return "" if exists.returncode else _git("show", f"{sha}:{path}")


def umbrella_appends(sha: str, base: str | None, body: str | None) -> tuple[str, ...]:
    """A refusal per umbrella contract this head adds an entry to (OMN-21301).

    Every pull request that appends to one contract conflicts every other at the same place, so
    each landing re-conflicts the rest: 25 of 31 conflicted omnimarket pull requests on
    2026-11-05. The comparison is against the merge base, so taking the base branch adds nothing.
    """
    base_sha = _resolve_base(base) if base is not None else None
    if base_sha is None:
        return (
            "umbrella contract appends cannot be checked: the base is unreadable "
            "and names no remote, and an unreadable comparison is a refusal",
        )
    try:
        fork = _git("merge-base", sha, base_sha)
    except subprocess.CalledProcessError:
        # A shallow base shares no history with the head: the base tip stands in.
        fork = base_sha
    messages = _git("log", "--format=%B", f"{base_sha}..{sha}")
    text = f"{body or os.environ.get(_BODY_ENV, '')}\n{messages}"
    failures = []
    for ticket in umbrella_ticket_set():
        path = f"contracts/{ticket}.yaml"
        added = sorted(_entry_ids(_blob(sha, path)) - _entry_ids(_blob(fork, path)))
        if added and umbrella_evidence(text) is None:
            failures.append(
                f"{path}: this head adds {len(added)} entries ({', '.join(added[:3])}) to an "
                "umbrella contract, which conflicts every other open pull request on it. "
                + PR_FIX.format(umbrella=ticket)
            )
    return tuple(failures)


def check(
    head: str, base: str | None, title: str | None = None, body: str | None = None
) -> tuple[str, ...]:
    """Read immutable git blobs, including when the working tree has a repair.

    ``base`` is None when the caller could not read it and it names no remote: a
    title taken from the commits cannot be built without it, and an explicit title
    still gets the contract refusal, with every comparison that needs the base
    failing closed.
    """
    sha = _git("rev-parse", "--verify", f"{head}^{{commit}}")
    if title is None:
        base = _resolve_base(base) if base is not None else None
        if base is None:
            raise subprocess.CalledProcessError(
                128, ["git", "rev-parse", "--verify", "<base>^{commit}"]
            )
        subjects = _git(
            "log",
            "--first-parent",
            "--no-merges",
            "--format=%s",
            *_own_revisions(sha, base),
        )
        branch = os.environ.get("PRE_COMMIT_LOCAL_BRANCH") or _git(
            "rev-parse", "--abbrev-ref", "HEAD"
        )
        title = f"{branch.upper()}\n{subjects}"
    tickets = cited_tickets(title)
    if not tickets:
        # Unticketed pushes (e.g. integration branches) do not invent a ticket.
        # CI's explicit PR title still must cite one, as receipt-gate requires.
        return ()
    failures = []
    release_cut: bool | None = None
    changed: tuple[str, ...] | None = None
    for ticket in tickets:
        path = f"contracts/{ticket}.yaml"
        exists = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}:{path}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        # Unreadable objects never count as a present contract.
        text = None if exists.returncode else _git("show", f"{sha}:{path}")
        failure = contract_prerequisite(ticket, text)
        if failure and text is None:
            if release_cut is None:
                release_cut = _is_release_cut(sha, base)
            if release_cut:
                continue
        if failure:
            failures.append(failure)
            continue
        if text is None:
            continue
        if changed is None:
            changed = _changed_paths(sha, base)
        proof = check_integration_proof(
            ModelIntegrationProofRequest(
                ticket_id=ticket, contract_text=text, changed_paths=changed
            )
        )
        if proof.failure:
            failures.append(proof.failure)
    if not failures:
        failures.extend(umbrella_appends(sha, base, body))
    return tuple(failures)


class HandlerTicketContractPrerequisite:
    """Run the declared check, fetching an absent base without executing evidence."""

    def handle(
        self, request: ModelTicketContractPrerequisiteRequest
    ) -> ModelTicketContractPrerequisiteResult:
        try:
            if request.title is not None and not cited_tickets(request.title):
                return ModelTicketContractPrerequisiteResult(
                    failures=(
                        "pull request title must cite at least one OMN-[0-9]+ ticket",
                    ),
                )
            sha = _git("rev-parse", "--verify", f"{request.head}^{{commit}}")
            # Resolve deployment refs at the effect boundary. The check helper
            # retains its conservative refusal when a comparison is unreadable.
            base = _resolve_base(request.base)
            if base is None and request.title is None:
                _git("rev-parse", "--verify", f"{request.base}^{{commit}}")
            failures = check(sha, base, request.title, request.body)
            return ModelTicketContractPrerequisiteResult(
                head_sha=sha, failures=failures
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return ModelTicketContractPrerequisiteResult(
                failures=(
                    f"ticket contract preflight could not read the pushed commit: {exc}",
                ),
            )
