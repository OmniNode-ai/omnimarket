# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Effect-boundary adapters for the contractor integration note (OMN-17277).

Two boundaries, each behind a Protocol so the handler stays testable with no
network and no checkout:

``ProtocolLinearNoteBoundary`` — read the ticket, read the notes already on it,
write one comment. The concrete Linear GraphQL adapter lives in the EFFECT
handler module so the imperative-contract guard can see the I/O boundary.

``ProtocolReleaseStateProbe`` — answer "is this merge commit in a released tag",
which is the difference between "you can use it now" and "here is the pin you
need". Answered from git, because the tag graph is the authority on it; a
release note or a version file states an intent, the tag states the fact.

The Linear credential is never a bare literal here: the ref name is read from
the node's own ``contract.yaml`` ``secrets`` block via ``contract_secret_ref``,
and the value is read from the process environment the secret store populates.
The endpoint is likewise resolved from the service-endpoint authority
(``configs/service_endpoints.yaml``), not written as a URL literal.

Related:
    - OMN-17277: integration note (WS2)
    - OMN-12856: contract-declared secret refs
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol

from omnimarket.nodes.node_contractor_integration_note_effect.models.model_integration_note_request import (
    ModelTicketFacts,
)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
LINEAR_SECRET_NAME = "LINEAR_API_KEY"


class ProtocolLinearNoteBoundary(Protocol):
    """Effect boundary for the Linear side of an integration note."""

    def fetch_ticket(self, identifier: str) -> ModelTicketFacts | None:
        """Resolve a ticket key (``OMN-123``) to its id and assignee."""

    def existing_note_keys(self, issue_id: str) -> tuple[str, ...]:
        """Return the integration-note keys already posted on this ticket."""

    def post_note(self, issue_id: str, body: str) -> None:
        """Write one comment carrying the note."""


class ProtocolReleaseStateProbe(Protocol):
    """Effect boundary for "is this commit in a released tag"."""

    def tags_containing(self, merge_sha: str) -> tuple[str, ...]:
        """Return the release tags whose history contains ``merge_sha``."""


class GitReleaseStateProbe:
    """Concrete ``ProtocolReleaseStateProbe`` over ``git tag --contains``.

    ``repo_path`` is supplied by the caller (the workflow's own checkout). No
    default is inferred from the environment: guessing a repository root is how
    a probe ends up answering about the wrong tree, and "no tags found" then
    silently reads as "not released".
    """

    def __init__(self, repo_path: Path) -> None:
        self._repo_path = repo_path

    def tags_containing(self, merge_sha: str) -> tuple[str, ...]:
        completed = subprocess.run(
            ["git", "-C", str(self._repo_path), "tag", "--contains", merge_sha],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "git tag --contains failed for "
                f"{merge_sha} in {self._repo_path}: {completed.stderr.strip()}"
            )
        return tuple(
            line.strip() for line in completed.stdout.splitlines() if line.strip()
        )


__all__ = [
    "CONTRACT_PATH",
    "LINEAR_SECRET_NAME",
    "GitReleaseStateProbe",
    "ProtocolLinearNoteBoundary",
    "ProtocolReleaseStateProbe",
]
