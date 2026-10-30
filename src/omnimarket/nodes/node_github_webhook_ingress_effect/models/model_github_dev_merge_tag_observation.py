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
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

GITHUB_DEV_MERGE_TAG_TOPIC_V1 = "onex.evt.github.dev-merge-tag.v1"  # onex-topic-allow: declared in node_github_webhook_ingress_effect contract.yaml publish_topics, consumed by node_receipt_gate_pin_advance_effect (OMN-20134)


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
