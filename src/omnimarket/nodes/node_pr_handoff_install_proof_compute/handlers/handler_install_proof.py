# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide whether a PR's changed files need an install proof, and whether one is given (OMN-23616).

The install-first rule (OMN-21815): a PR that changes what runs on a host (a host install, a launchd or systemd
unit, a tool, a hook or a plugin) is handed off only when its Lab line or a STATUS row names the branch revision
installed on a lab host and what was observed running. The path lists, exclusions and observation words are the
deployment overlay's declaration (ModelInstallDeclaration); this module holds no policy value. A declaration that
cannot be read REFUSES the decision; it never reads as "nothing required".

Ported from the frozen plugin's node_pr_handoff_lab_exempt_compute (install_files, observation_problem,
find_install_proof, install_message), minus its file reads: the caller passes the changed names, the proof sources
and the ancestry answers it already read.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable, Iterable

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.pr_handoff_install_proof import (
    ModelInstallDeclaration,
    ModelInstallProofRequest,
    ModelInstallProofResult,
    ModelInstallProofSource,
)
from omnimarket.nodes.node_pr_handoff_install_proof_compute.overlay import (
    load_install_declaration,
)

INSTALLED_RE = re.compile(
    r"(?<![\w-])installed=([A-Za-z0-9][A-Za-z0-9._-]*):([0-9a-f]{7,40})(?![0-9A-Za-z])"
)
OBSERVED_RE = re.compile(r"(?<![\w-])observed=(.*?)(?=\s\|\s|$)")
LAB_LINE_RE = re.compile(r"(?im)^\W*lab\b.*$")
HEAD_RE = re.compile(r"\bhead=([0-9a-f]{7,40})(?![0-9A-Za-z])")
STATUS_SOURCE = "status"

# (installed revision, live head) -> (verdict, why): "yes" when the head is the revision or in its history, "no" when the
# revision does not contain it, "unread" when it could not be read. The pure node takes the answers in the request.
Ancestry = Callable[[str, str], tuple[str, str]]


def match(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def install_files(
    files: Iterable[str], repo: str, declaration: ModelInstallDeclaration
) -> tuple[str, ...]:
    """The changed files that change what runs on a host: a declared path (or the repository's own), no excluded
    file, no prod path."""
    patterns = (*declaration.paths, *declaration.repo_paths.get(repo, ()))
    return tuple(
        name
        for name in files
        if not match(name, declaration.excluded_paths)
        and not match(name, declaration.prod_paths)
        and match(name, patterns)
    )


def observation_problem(observed: str, declaration: ModelInstallDeclaration) -> str:
    """Why an observation does not show the installed change running, else an empty string."""
    text = observed.strip()
    if len(text) < declaration.min_observed_chars:
        return "the observation is empty or too short"
    if any(re.search(rx, text, re.IGNORECASE) for rx in declaration.observed_refused):
        return "the observation is only that tests passed"
    lowered = text.lower()
    if not any(
        re.search(r"(?<![a-z])" + re.escape(word.lower()) + r"(?![a-z])", lowered)
        for word in declaration.observed_words
    ):
        return "the observation does not say the installed change ran (a tick, a call, a hook firing, a loaded unit)"
    return ""


def find_install_proof(
    sources: Iterable[ModelInstallProofSource],
    head: str,
    declaration: ModelInstallDeclaration,
    ancestry: Ancestry,
) -> tuple[str, str]:
    """(``installed=<host>:<rev> source=<name>`` for the first proof, else ``""``; the reason the nearest candidate
    failed). A Lab line counts only when its own head= cell is a prefix of the live head; a status row when its
    installed revision is. The installed revision is a prefix of the live head, or a revision that has the live
    head as an ancestor (``ancestry`` answers "yes"). One that does not contain the head, one that cannot be read
    (unread) and one that holds only an older head of the PR never pass."""
    reason = "no Lab line or STATUS row carries installed=<host>:<revision>"
    for source in sources:
        lines = (
            [source.text]
            if source.name == STATUS_SOURCE
            else LAB_LINE_RE.findall(source.text)
        )
        for line in lines:
            found = INSTALLED_RE.search(line)
            if not found:
                continue
            host, rev = found.groups()
            contains = ""
            if not head.startswith(rev):
                verdict, why = ancestry(rev, head)
                if verdict != "yes":
                    reason = (
                        f"installed={host}:{rev} is not the live head {head[:7]} and "
                        + (
                            f"its ancestry is UNREAD ({why})"
                            if verdict == "unread"
                            else "does not contain it"
                        )
                        + ": install the pushed head"
                    )
                    continue
                contains = f" contains-head={head[:7]}"
            heads = HEAD_RE.findall(line)
            if (
                source.name != STATUS_SOURCE
                and heads
                and not any(head.startswith(h) for h in heads)
            ):
                reason = f"the Lab line names head {heads[0][:7]}, not the live head {head[:7]}"
                continue
            seen = OBSERVED_RE.search(line)
            observed = (
                INSTALLED_RE.sub("", HEAD_RE.sub("", seen.group(1))) if seen else ""
            )
            problem = observation_problem(observed, declaration)
            if problem:
                reason = f"installed={host}:{rev} names no run: {problem}"
                continue
            return f"installed={host}:{rev} source={source.name}{contains}", ""
    return "", reason


def install_message(required: tuple[str, ...], reason: str) -> str:
    shown = ", ".join(required[:3]) + (" and more" if len(required) > 3 else "")
    return (
        f"this PR changes what runs on a host ({shown}) and its proof is not enough: {reason}. Install the pushed "
        "branch revision on the lab through the declared installer, watch it run, then comment on the PR "
        '"Lab: head=<sha> host=<host> installed=<host>:<sha> observed=<what ran>" or write a STATUS row with '
        "installed=<host>:<sha> observed=<what ran>"
    )


class HandlerInstallProof:
    """Decide whether a PR's changed files require an install proof and whether one is given."""

    def __init__(
        self,
        declaration_loader: Callable[[], ModelInstallDeclaration] | None = None,
    ) -> None:
        self._declaration_loader = declaration_loader or load_install_declaration

    def handle(self, request: ModelInstallProofRequest) -> ModelInstallProofResult:
        try:
            declaration = self._declaration_loader()
        except (NodeOverlayError, ValidationError) as exc:
            return ModelInstallProofResult(
                verdict="REFUSED",
                detail=f"the host_install overlay cannot be read: {type(exc).__name__}",
            )
        if request.exempt_kind:
            return ModelInstallProofResult(
                verdict="NA", detail=f"the PR is an exempt kind ({request.exempt_kind})"
            )
        required = install_files(request.files, request.repo, declaration)
        if not required:
            return ModelInstallProofResult(
                verdict="NA",
                detail="no changed file is a host install, unit, tool, hook or plugin",
            )
        answers = {(a.rev, a.head): (a.verdict, a.why) for a in request.ancestry}

        def ancestry(rev: str, head: str) -> tuple[str, str]:
            return answers.get((rev, head), ("unread", "no ancestry answer"))

        found, reason = find_install_proof(
            request.sources, request.head, declaration, ancestry
        )
        if found:
            return ModelInstallProofResult(verdict="OK", detail=found)
        return ModelInstallProofResult(
            verdict="NO",
            detail=install_message(required, reason),
            required_files=required,
        )
