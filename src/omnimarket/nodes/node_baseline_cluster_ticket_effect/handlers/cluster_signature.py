# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stable signature of one dev-baseline cluster: exact lines a description carries, never the title.

The dev-baseline reporter files a ticket for every failing cluster it cannot find again, so a
report is matched by what a description says, line for line: the signature line, or the legacy
two-line form the old reporter filed. Lines compare for equality after ``.strip()`` and never as
substrings, so a longer cluster name does not match a shorter one's description.
"""

from __future__ import annotations

import re

SIGNATURE_PREFIX = "Cluster signature: dev-baseline:"
REPO_LABEL = "**Repo:**"
CLUSTER_LABEL = "**Cluster:**"
LEGACY_TRAILING_SPACES = "  "

_NUMBER = re.compile(r"([0-9]+)$")


def signature_line(repo: str, cluster: str) -> str:
    """The one exact line that names the cluster: ``Cluster signature: dev-baseline:<repo>:<cluster>``."""
    return f"{SIGNATURE_PREFIX}{repo}:{cluster}"


def legacy_lines(repo: str, cluster: str) -> tuple[str, str]:
    """The two lines the old reporter filed: the repo line, then the cluster line, each with two trailing spaces."""
    return (
        f"{REPO_LABEL} `{repo}`{LEGACY_TRAILING_SPACES}",
        f"{CLUSTER_LABEL} `{cluster}`{LEGACY_TRAILING_SPACES}",
    )


def search_needles(repo: str, cluster: str) -> tuple[str, str]:
    """The needles the tracker's ``description contains`` search runs: the signature line, then the legacy cluster line."""
    return (signature_line(repo, cluster), f"{CLUSTER_LABEL} `{cluster}`")


def carries_signature(description: str | None, repo: str, cluster: str) -> bool:
    """Whether the description carries the signature: a line equal to it, or both legacy lines (each read stripped)."""
    wanted = signature_line(repo, cluster)
    legacy_repo, legacy_cluster = legacy_lines(repo, cluster)
    saw_repo = False
    saw_cluster = False
    for line in (description or "").splitlines():
        stripped = line.strip()
        if stripped == wanted:
            return True
        saw_repo = saw_repo or stripped == legacy_repo.strip()
        saw_cluster = saw_cluster or stripped == legacy_cluster.strip()
    return saw_repo and saw_cluster


def oldest_first(tickets: tuple[str, ...]) -> tuple[str, ...]:
    """The tickets in creation order: the lowest number first, an id with no number after the others."""

    def number(ticket: str) -> tuple[int, int, str]:
        match = _NUMBER.search(ticket)
        return (0, int(match.group(1)), ticket) if match else (1, 0, ticket)

    return tuple(sorted(dict.fromkeys(tickets), key=number))
