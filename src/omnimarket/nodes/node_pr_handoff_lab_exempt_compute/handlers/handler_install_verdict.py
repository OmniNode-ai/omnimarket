# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The install-first verdict pr-handoff judges a PR with (OMN-21815, moved from the frozen plugin, OMN-19998).

A PR that changes what runs on a host (a host install, a launchd or systemd unit, a tool, a hook or a plugin) is
handed off only when its Lab line, a lab comment of its author or the write node, or a STATUS row of the ledger names
the branch revision installed on a lab host and what was observed running. Every list (the paths, the excluded
paths, the prod paths, the observation words) is the ``host_install`` overlay document; nothing here lists a path.
The verdict is pure: every read (the declaration, the changed files, the comments, git ancestry) is the effect's.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Mapping, Sequence

from omnimarket.nodes.node_pr_handoff_lab_exempt_compute.models.model_install_declaration import (
    ModelPrHandoffInstallDeclaration,
)
from omnimarket.nodes.node_pr_handoff_lab_exempt_compute.models.model_install_verdict import (
    ModelInstallAncestry,
    ModelInstallChangedFile,
    ModelInstallSource,
    ModelInstallVerdict,
    ModelInstallVerdictRequest,
)

# The grammar of the proof line, not deployment values.
INSTALLED_RE = re.compile(
    r"(?<![\w-])installed=([A-Za-z0-9][A-Za-z0-9._-]*):([0-9a-f]{7,40})(?![0-9A-Za-z])"
)
OBSERVED_RE = re.compile(r"(?<![\w-])observed=(.*?)(?=\s\|\s|$)")
LAB_LINE_RE = re.compile(r"(?im)^\W*lab\b.*$")
HEAD_RE = re.compile(r"\bhead=([0-9a-f]{7,40})(?![0-9A-Za-z])")

UNREAD = ModelInstallAncestry(verdict="unread", why="the revision was not read")


def match(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def install_files(
    files: Sequence[ModelInstallChangedFile],
    repo: str,
    decl: ModelPrHandoffInstallDeclaration,
) -> list[str]:
    """The changed files that change what runs on a host: a declared path (or the repository's own), no excluded
    path (tests, documents, the CI-only folders), no prod path. A renamed file counts under both names."""
    names = [n for f in files for n in (f.filename, f.previous_filename) if n]
    patterns = (*decl.paths, *decl.repo_paths.get(repo, ()))
    return [
        n
        for n in names
        if not match(n, decl.excluded_paths)
        and not match(n, decl.prod_paths)
        and match(n, patterns)
    ]


def observation_problem(observed: str, decl: ModelPrHandoffInstallDeclaration) -> str:
    """Why an observation does not show the installed change running, else an empty string."""
    text = observed.strip()
    if len(text) < decl.min_observed_chars:
        return "the observation is empty or too short"
    if any(re.search(pattern, text, re.I) for pattern in decl.observed_refused):
        return "the observation is only that tests passed"
    lowered = text.lower()
    if not any(
        re.search(r"(?<![a-z])" + re.escape(w.lower()) + r"(?![a-z])", lowered)
        for w in decl.observed_words
    ):
        return "the observation does not say the installed change ran (a tick, a call, a hook firing, a loaded unit)"
    return ""


def _proof_lines(source: ModelInstallSource) -> list[str]:
    return (
        [source.text] if source.name == "status" else LAB_LINE_RE.findall(source.text)
    )


def candidate_revisions(sources: Iterable[ModelInstallSource], head: str) -> list[str]:
    """The installed revisions the sources name that are not a prefix of the live head, in order, once each: the
    revisions whose ancestry the effect must read."""
    out: list[str] = []
    for source in sources:
        for line in _proof_lines(source):
            found = INSTALLED_RE.search(line)
            if (
                found
                and not head.startswith(found.group(2))
                and found.group(2) not in out
            ):
                out.append(found.group(2))
    return out


def find_install_proof(
    sources: Iterable[ModelInstallSource],
    head: str,
    decl: ModelPrHandoffInstallDeclaration,
    ancestry: Mapping[str, ModelInstallAncestry],
) -> tuple[str, str]:
    """(``installed=<host>:<rev> source=<name>`` for the first proof, else ``""``; the reason the nearest candidate
    failed). A Lab line counts only when its own head= cell is a prefix of the live head. The installed revision is a
    prefix of the live head or a revision git says holds it; a revision that does not contain the head, one that
    could not be read and one that holds only an older head of the PR never pass."""
    reason = (
        "no Lab line, lab comment or STATUS row carries installed=<host>:<revision>"
    )
    for source in sources:
        for line in _proof_lines(source):
            found = INSTALLED_RE.search(line)
            if not found:
                continue
            host, rev = found.groups()
            contains = ""
            if not head.startswith(rev):
                read = ancestry.get(rev, UNREAD)
                if read.verdict != "yes":
                    why = (
                        f"its ancestry is UNREAD ({read.why})"
                        if read.verdict == "unread"
                        else "does not contain it"
                    )
                    reason = f"installed={host}:{rev} is not the live head {head[:7]} and {why}: install the pushed head"
                    continue
                contains = f" contains-head={head[:7]}"
            heads = HEAD_RE.findall(line)
            if (
                source.name != "status"
                and heads
                and not any(head.startswith(h) for h in heads)
            ):
                reason = f"the Lab line names head {heads[0][:7]}, not the live head {head[:7]}"
                continue
            seen = OBSERVED_RE.search(line)
            observed = (
                INSTALLED_RE.sub("", HEAD_RE.sub("", seen.group(1))) if seen else ""
            )
            problem = observation_problem(observed, decl)
            if problem:
                reason = f"installed={host}:{rev} names no run: {problem}"
                continue
            return f"installed={host}:{rev} source={source.name}{contains}", ""
    return "", reason


def status_rows(ledger: str, lane: str, ticket: str) -> list[str]:
    """The STATUS rows of the ledger that carry an installed= cell for the handing-off lane or the ticket."""
    out: list[str] = []
    for row in ledger.splitlines():
        cells = [c.strip() for c in row.split(" | ")]
        if (
            len(cells) > 2
            and cells[1] == "STATUS"
            and any(c.startswith("installed=") for c in cells)
            and (f"lane={lane}" in cells or f"ticket={ticket}" in cells)
        ):
            out.append(row)
    return out


def install_message(files: Sequence[str], reason: str) -> str:
    shown = ", ".join(files[:3]) + (" and more" if len(files) > 3 else "")
    return (
        f"this PR changes what runs on a host ({shown}) and its proof is not enough: {reason}. Install the pushed branch "
        "revision on the lab through the declared installer, watch it run, then comment on the PR "
        '"Lab: head=<sha> host=<host> installed=<host>:<sha> observed=<what ran>" or write a STATUS row with '
        "installed=<host>:<sha> observed=<what ran> (/omni:pr-handoff, install first, OMN-21815)"
    )


def decide_install_proof(request: ModelInstallVerdictRequest) -> ModelInstallVerdict:
    decl, head = request.declaration, request.head
    if request.lab_exempt_kind:
        return ModelInstallVerdict(
            status="NA", detail=f"the PR is an exempt kind ({request.lab_exempt_kind})"
        )
    found, reason = find_install_proof(request.sources, head, decl, request.ancestry)
    if found:
        return ModelInstallVerdict(status="OK", detail=found)
    required = install_files(request.files, request.repo, decl)
    if not required:
        return ModelInstallVerdict(
            status="NA",
            detail="no changed file is a host install, unit, tool, hook or plugin",
        )
    found, why = find_install_proof(
        request.comment_sources, head, decl, request.ancestry
    )
    if found:
        return ModelInstallVerdict(status="OK", detail=found)
    detail = install_message(required, why if request.comment_sources else reason)
    return ModelInstallVerdict(
        status="NO", detail=detail, required_files=tuple(required)
    )


class HandlerInstallVerdict:
    """The canonical handler: ``handle(request) -> verdict``."""

    def handle(self, request: ModelInstallVerdictRequest) -> ModelInstallVerdict:
        return decide_install_proof(request)
