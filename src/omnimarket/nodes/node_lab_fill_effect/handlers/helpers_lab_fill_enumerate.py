# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure readers of ticket and acceptance text for the lab-fill enumerate operations (OMN-23250).

Moved unchanged from the frozen repository's lab-fill selection handler: the commands and paths an
acceptance check names, and the repository a ticket description declares on a Repo line.
"""

from __future__ import annotations

import re

# What an acceptance check names that can be run or looked up: the executable it starts (`uv` for a
# `uv run` line, else a well-known tool) and a path of two or more segments (OMN-20721).
_ACCEPTANCE_COMMAND = re.compile(
    r"\b(uv) run\b"
    r"|(?<!uv run )\b(pytest|curl|python3?|ruff|mypy|bash|git|docker|kubectl|psql|rpk"
    r"|npm|npx|node|make|onex[\w-]*)\b"
)
_ACCEPTANCE_PATH = re.compile(
    r"(?<![\w/.$~-])((?:\$[A-Z_]+/|~/|\./)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)"
)

# A Repo line of a ticket description: "**Repo:** omnibase_infra.", "Repo omnimarket. Files: ...",
# "**Repo / surface.** `omnibase_infra` - ...". A table header ("| Repo | PR |") starts with a bar
# and declares nothing.
_REPO_LINE = re.compile(
    r"^(?:repo|repository)(?:\s*/\s*surface)?(?![A-Za-z0-9])[\s*_.:`=-]*([A-Za-z0-9][A-Za-z0-9_.-]*)",
    re.I,
)


_PR_URL = re.compile(r"github[.]com/[^/]+/([^/]+)/pull/([0-9]+)")
_PR_TITLE_KIND = re.compile(r"([a-z]+)(?:[(][^)]*[)])?!?:")
IMPLEMENTATION_TYPES = ("feat", "fix", "refactor", "perf")


def attached_prs(attachments: list[dict[str, object]]) -> tuple[str, ...]:
    """The PRs a ticket's GitHub attachments link, newest first: where its work has lived (OMN-17427)."""
    found: list[tuple[str, str]] = []
    for attachment in attachments:
        match = _PR_URL.search(str(attachment.get("url") or ""))
        if match:
            found.append(
                (str(attachment.get("createdAt") or ""), f"{match[1]}#{match[2]}")
            )
    return tuple(pr for _, pr in sorted(found, reverse=True))


def merged_implementations(
    attachments: list[dict[str, object]], has_children: bool
) -> tuple[str, ...]:
    """The merged implementations the brief check reads on a ticket with no children (OMN-20864): a GitHub
    attachment whose status is merged and whose title is a feat, fix, refactor or perf change."""
    if has_children:
        return ()
    found: set[str] = set()
    for attachment in attachments:
        kind = _PR_TITLE_KIND.match(str(attachment.get("title") or ""))
        match = _PR_URL.search(str(attachment.get("url") or ""))
        meta = attachment.get("metadata")
        status = meta.get("status") if isinstance(meta, dict) else None
        if (
            attachment.get("sourceType") == "github"
            and status == "merged"
            and kind
            and kind[1] in IMPLEMENTATION_TYPES
            and match
        ):
            found.add(f"{match[1]}#{match[2]}")
    return tuple(sorted(found))


def acceptance_references(text: str) -> tuple[str, ...]:
    """The commands and paths an acceptance check names, in order and without repeats."""
    found = [m[0] or m[1] for m in _ACCEPTANCE_COMMAND.findall(text)]
    found += _ACCEPTANCE_PATH.findall(text)
    return tuple(dict.fromkeys(found))


def declared_repository(description: str, fleet: set[str]) -> str:
    """The fleet repository a ticket description declares on a Repo line, or "" (OMN-20842).

    Only a name in the fleet registry counts, and two different declarations declare nothing."""
    known = {name.lower(): name for name in fleet}
    found = set()
    for line in description.splitlines():
        match = _REPO_LINE.match(re.sub(r"^[\s>*_#-]+", "", line))
        if match and (name := known.get(match[1].rstrip(".-").lower())):
            found.add(name)
    return next(iter(found)) if len(found) == 1 else ""
