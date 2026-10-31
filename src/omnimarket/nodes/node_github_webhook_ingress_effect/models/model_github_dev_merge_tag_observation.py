# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A dev-merge tag pushed to a repository, folded from a GitHub push delivery (OMN-20134).

Published on ``onex.evt.github.dev-merge-tag.v1``. Every dev merge of an
OmniNode repository is tagged ``dev-<UTC 14 digits>-<sha12>``; the tag is the
ref a consumer pins, so its creation is the event that tells a consumer a newer
pin exists.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_section,
)


def _dev_merge_tag_topic(contract_path: Path) -> str:
    """Resolve this observation's unique, declared publication topic."""
    events = contract_section(contract_path, "published_events")
    if isinstance(events, list):
        topics = [
            event.get("topic")
            for event in events
            if isinstance(event, dict)
            and event.get("event_type") == "GitHubDevMergeTagObservation"
        ]
        if (
            len(topics) == 1
            and isinstance(topics[0], str)
            and topics[0] in contract_publish_topics(contract_path)
        ):
            return topics[0]
    raise ValueError(
        f"{contract_path} must declare one published GitHubDevMergeTagObservation topic"
    )


GITHUB_DEV_MERGE_TAG_TOPIC_V1 = _dev_merge_tag_topic(
    Path(__file__).resolve().parents[1] / "contract.yaml"
)


class ModelGitHubDevMergeTagObservation(BaseModel):
    """One dev-merge tag creation."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    topic: str = GITHUB_DEV_MERGE_TAG_TOPIC_V1
    repo: str = Field(..., min_length=3, description="'<owner>/<repo>'.")
    tag: str = Field(..., pattern=r"^dev-\d{14}-[0-9a-f]{12}$")
    sha: str = Field(..., pattern=r"^[0-9a-f]{40}$", description="The tagged commit.")
    delivery_id: UUID = Field(..., description="The X-GitHub-Delivery GUID.")
    github_event: str = Field(..., min_length=1)
    as_of: datetime = Field(..., description="When GitHub says the tag was pushed.")


__all__: list[str] = ["ModelGitHubDevMergeTagObservation"]
