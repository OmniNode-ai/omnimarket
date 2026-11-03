# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only git boundary for the ticket-contract prerequisite (OMN-20864)."""

from __future__ import annotations

import os
import re
import subprocess

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


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, timeout=60
    ).stdout.strip()


# The receipt gate admits a release cut only while its diff against the merge base
# stays inside these root paths, so a cut cannot carry a contracts/ file.
_RELEASE_CUT_PATHS = frozenset({"CHANGELOG.md", "pyproject.toml", "uv.lock"})


def _is_release_cut(sha: str, base: str) -> bool:
    """Whether the diff against the merge base is a root changelog plus optional pins."""
    try:
        merge_base = _git("merge-base", base, sha)
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


def _changed_paths(sha: str, base: str) -> tuple[str, ...]:
    """The paths this push's own commits change; unreadable fails closed.

    A merge commit contributes only what its resolution added beyond both
    parents (``--cc``), so a clean merge of dev adds nothing and a conflict
    resolved into new code still counts.
    """
    try:
        base_sha = _git("rev-parse", "--verify", f"{base}^{{commit}}")
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


def check(head: str, base: str, title: str | None = None) -> tuple[str, ...]:
    """Read immutable git blobs, including when the working tree has a repair."""
    sha = _git("rev-parse", "--verify", f"{head}^{{commit}}")
    if title is None:
        base_sha = _git("rev-parse", "--verify", f"{base}^{{commit}}")
        subjects = _git(
            "log",
            "--first-parent",
            "--no-merges",
            "--format=%s",
            *_own_revisions(sha, base_sha),
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
    return tuple(failures)


class HandlerTicketContractPrerequisite:
    """Run the contract-declared check without executing evidence or writing git."""

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
            failures = check(sha, request.base, request.title)
            return ModelTicketContractPrerequisiteResult(
                head_sha=sha, failures=failures
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return ModelTicketContractPrerequisiteResult(
                failures=(
                    f"ticket contract preflight could not read the pushed commit: {exc}",
                ),
            )
