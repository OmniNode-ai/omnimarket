# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shared-cause key: one copy for every reader of a red check's failure annotation.

The landing decision node and the bus red-CI triage both key a shared cause as
``cause:<owner>/<repo>:<sig12>``, where the signature folds the check name and
its first failure annotation with everything that varies per PR or per run
replaced. ``first_failure_annotations`` picks that annotation out of the
GitHub check rollup read by ``annotation_query``; both are pure.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

CAUSE_PREFIX = "cause:"
UNREAD = "unread"
SIGNATURE_TEXT_LIMIT = 300

# A check-run conclusion whose annotations are read, and the per-annotation cut.
ANNOTATION_FAILING = frozenset(
    {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED", "CANCELLED"}
)
ANNOTATION_MAX_CHARS = 1000
# PRs per annotation_query: a deeper or wider query hits GitHub's GraphQL resource limits.
ANNOTATION_CHUNK = 6

_GENERIC_EXIT = re.compile(r"^Process completed with exit code \d+\.?$")
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_SHA = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,64}\b")
_PR_NUMBER = re.compile(r"#\d+")
_BRACKETED = re.compile(r"\[[^\[\]]*\]")
_DIGITS = re.compile(r"\d+")
_SPACE = re.compile(r"\s+")


def cause_key(repo: str, signature: str) -> str:
    """``cause:<owner>/<repo>:<signature>``."""
    return f"{CAUSE_PREFIX}{repo}:{signature}"


def normalize_annotation(text: str) -> str:
    """A failure annotation with everything that varies per PR or per run replaced.

    The generic "Process completed with exit code N" line is skipped;
    timestamps, shas, ``#n`` references, bracketed id lists and digit runs are
    replaced; whitespace is collapsed; the result is cut to 300 characters.
    """
    lines = (line.strip() for line in text.splitlines())
    out = " ".join(line for line in lines if line and not _GENERIC_EXIT.match(line))
    out = _TIMESTAMP.sub("<ts>", out)
    out = _SHA.sub("<sha>", out)
    out = _PR_NUMBER.sub("#<n>", out)
    out = _BRACKETED.sub("[<ids>]", out)
    out = _DIGITS.sub("<n>", out)
    return _SPACE.sub(" ", out).strip()[:SIGNATURE_TEXT_LIMIT]


def normalize_signature(check: str, text: str | None) -> str:
    """The failure signature of one red check: 12 hex of sha256(check, normalized text).

    A check with no annotation, or one that normalizes to nothing, is
    ``unread`` and never clusters.
    """
    if text is None:
        return UNREAD
    normalized = normalize_annotation(text)
    if not normalized:
        return UNREAD
    return hashlib.sha256(f"{check}\n{normalized}".encode()).hexdigest()[:12]


def annotation_query(prs: Sequence[tuple[str, int]]) -> str:
    """One GraphQL document reading each PR's head check contexts and their first three annotations.

    ``prs`` are ``(owner/repo, number)`` pairs; PR ``i`` is aliased ``a<i>``.
    """
    parts = []
    for i, (slug, number) in enumerate(prs):
        owner, _, name = slug.partition("/")
        parts.append(
            f'a{i}: repository(owner: "{owner}", name: "{name}") {{ pullRequest(number: {int(number)}) {{ number headRefOid '
            "statusCheckRollup: commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 100) { nodes { "
            "... on CheckRun { name conclusion databaseId annotations(first: 3) { nodes { message annotationLevel } } } "
            "} } } } } } } }"
        )
    return "query { " + " ".join(parts) + " }"


def first_failure_annotations(
    pr: Mapping[str, Any] | None, head: str
) -> dict[str, str] | None:
    """``{check name: first failure annotation}`` for the failing checks of one PR node read at ``head``.

    The node is the ``pullRequest`` object of ``annotation_query``. Per check
    name, the first FAILURE-level annotation that is not the runner's generic
    exit-code line, over the failing check runs of that name in GitHub's order;
    a failing check with none maps to ``""`` (unread: it never clusters). None
    when the node is missing or was read at another head (no fact from a stale
    read).
    """
    if not isinstance(pr, Mapping) or str(pr.get("headRefOid") or "") != head:
        return None
    out: dict[str, str] = {}
    try:
        commits = pr["statusCheckRollup"]["nodes"]
        rollup = commits[-1]["commit"].get("statusCheckRollup") or {}
        contexts = rollup.get("contexts", {}).get("nodes") or []
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    for ctx in contexts:
        if (
            not isinstance(ctx, Mapping)
            or str(ctx.get("conclusion") or "").upper() not in ANNOTATION_FAILING
        ):
            continue
        name = str(ctx.get("name") or "")
        if not name or out.get(name):
            continue
        text = ""
        for annotation in (ctx.get("annotations") or {}).get("nodes") or ():
            message = str((annotation or {}).get("message") or "").strip()
            level = str((annotation or {}).get("annotationLevel") or "").upper()
            if level == "FAILURE" and message and not _GENERIC_EXIT.match(message):
                text = message[:ANNOTATION_MAX_CHARS]
                break
        out[name] = text
    return dict(sorted(out.items()))


__all__: list[str] = [
    "ANNOTATION_CHUNK",
    "ANNOTATION_FAILING",
    "ANNOTATION_MAX_CHARS",
    "CAUSE_PREFIX",
    "SIGNATURE_TEXT_LIMIT",
    "UNREAD",
    "annotation_query",
    "cause_key",
    "first_failure_annotations",
    "normalize_annotation",
    "normalize_signature",
]
