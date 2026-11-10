# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The install-proof read (OMN-21815): load the overlay declaration, read the ledger and git ancestry, ask the verdict.

Every read is here and the decision is the compute node's. An unreadable declaration refuses (``NO``, ``refused``):
the frozen plugin passes any first word but ``NO``, so a refusal that said anything else would pass.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_pr_handoff_lab_exempt_compute.handlers.handler_install_verdict import (
    HandlerInstallVerdict,
    candidate_revisions,
    status_rows,
)
from omnimarket.nodes.node_pr_handoff_lab_exempt_compute.models.model_install_verdict import (
    ModelInstallAncestry,
    ModelInstallSource,
    ModelInstallVerdictRequest,
)
from omnimarket.nodes.node_pr_handoff_reads_effect.models.model_install_proof_read import (
    ModelInstallProofReadRequest,
    ModelInstallProofReadResult,
)
from omnimarket.nodes.node_pr_handoff_reads_effect.overlay import (
    load_install_declaration,
)

AncestryReader = Callable[[str, str, str], ModelInstallAncestry]


def _git_env() -> dict[str, str]:
    import os

    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def git_ancestry(
    clone: str, rev: str, head: str, timeout: float = 30.0
) -> ModelInstallAncestry:
    """Whether the installed revision holds the live head, read in the clone: ``yes`` when ``git merge-base
    --is-ancestor <head> <rev>`` exits 0, ``no`` when it exits 1, ``unread`` for anything else (not a repository, an
    absent or ambiguous revision, a head the clone lacks, a timeout). Unread is never a pass."""

    def resolve(name: str) -> str:
        done = subprocess.run(
            [
                "git",
                "-C",
                clone,
                "rev-parse",
                "--verify",
                "--quiet",
                f"{name}^{{commit}}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=_git_env(),
        )
        return done.stdout.strip() if done.returncode == 0 else ""

    try:
        full = resolve(rev)
        if not full or not full.startswith(rev.lower()):
            return ModelInstallAncestry(
                verdict="unread",
                why=f"{rev} is not a commit the clone {Path(clone).name} holds",
            )
        if not resolve(head):
            return ModelInstallAncestry(
                verdict="unread",
                why=f"the clone {Path(clone).name} lacks the live head {head[:7]}",
            )
        done = subprocess.run(
            ["git", "-C", clone, "merge-base", "--is-ancestor", head, full],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ModelInstallAncestry(
            verdict="unread", why=f"git in {Path(clone).name}: {type(exc).__name__}"
        )
    if done.returncode in (0, 1):
        return ModelInstallAncestry(verdict="yes" if done.returncode == 0 else "no")
    return ModelInstallAncestry(
        verdict="unread", why=f"git merge-base exited {done.returncode}"
    )


def _norm(login: object) -> str:
    return re.sub(r"(?i)^app/|\[bot\]$", "", str(login or "").strip()).lower()


def comment_counts(commenter: str, author: str, write_logins: set[str]) -> bool:
    """A comment counts as the PR's own when its author wrote the PR or is a declared write-node login."""
    return bool(commenter) and (commenter == author or _norm(commenter) in write_logins)


class HandlerInstallProofRead:
    """``handle(request) -> result``; the environment names the overlay roots, git is the one injected read."""

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        ancestry: AncestryReader | None = None,
    ) -> None:
        self._environ = environ
        self._ancestry = ancestry or git_ancestry_reader

    def handle(
        self, request: ModelInstallProofReadRequest
    ) -> ModelInstallProofReadResult:
        try:
            declaration = load_install_declaration(self._environ)
        except NodeOverlayError as exc:
            return ModelInstallProofReadResult(
                status="NO",
                refused=True,
                detail=f"the host_install declaration cannot be read, so no PR passes install-first: {exc}",
            )
        try:
            ledger = (
                Path(request.ledger_path).read_text(encoding="utf-8", errors="replace")
                if request.ledger_path
                else ""
            )
        except OSError:
            ledger = ""
        sources = (
            ModelInstallSource(name="body", text=request.pr_body),
            *(
                ModelInstallSource(name="status", text=row)
                for row in status_rows(ledger, request.lane, request.ticket)
            ),
        )
        logins = {_norm(x) for x in request.write_logins if _norm(x)}
        comment_sources = tuple(
            ModelInstallSource(name="comment", text=c.body)
            for c in request.comments
            if comment_counts(c.login, request.pr_author, logins)
        )
        ancestry = {
            rev: self._ancestry(request.clone, rev, request.head)
            for rev in candidate_revisions((*sources, *comment_sources), request.head)
        }
        verdict = HandlerInstallVerdict().handle(
            ModelInstallVerdictRequest(
                declaration=declaration,
                repo=request.repo,
                head=request.head,
                lab_exempt_kind=request.lab_exempt_kind,
                sources=sources,
                comment_sources=comment_sources,
                files=request.files,
                ancestry=ancestry,
            )
        )
        return ModelInstallProofReadResult(
            status=verdict.status,
            detail=verdict.detail,
            required_files=verdict.required_files,
        )


def git_ancestry_reader(clone: str, rev: str, head: str) -> ModelInstallAncestry:
    return git_ancestry(clone, rev, head)
