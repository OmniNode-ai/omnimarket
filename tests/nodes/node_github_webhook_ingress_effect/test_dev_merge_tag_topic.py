# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev-merge-tag observation takes its publication topic from its contract."""

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_github_webhook_ingress_effect.models.model_github_dev_merge_tag_observation import (
    _dev_merge_tag_topic,
)

pytestmark = pytest.mark.unit


def test_topic_follows_the_named_event_when_publications_are_reordered(
    tmp_path: Path,
) -> None:
    path = tmp_path / "contract.yaml"
    topic = "contract-selected-topic"
    path.write_text(
        yaml.safe_dump(
            {
                "published_events": [
                    {"event_type": "OtherObservation", "topic": "other-topic"},
                    {"event_type": "GitHubDevMergeTagObservation", "topic": topic},
                ],
                "event_bus": {"publish_topics": [topic, "other-topic"]},
            }
        ),
        encoding="utf-8",
    )

    assert _dev_merge_tag_topic(path) == topic


@pytest.mark.parametrize(
    ("events", "publish_topics"),
    [
        ([], ["selected-topic"]),
        (
            [{"event_type": "GitHubDevMergeTagObservation", "topic": "selected-topic"}],
            [],
        ),
        (
            [{"event_type": "GitHubDevMergeTagObservation", "topic": "selected-topic"}]
            * 2,
            ["selected-topic"],
        ),
        (
            [{"event_type": "GitHubDevMergeTagObservation", "topic": None}],
            ["selected-topic"],
        ),
    ],
    ids=["missing-event", "unpublished-topic", "duplicate-event", "invalid-topic"],
)
def test_invalid_topic_binding_fails_closed(
    tmp_path: Path, events: object, publish_topics: object
) -> None:
    path = tmp_path / "contract.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "published_events": events,
                "event_bus": {"publish_topics": publish_topics},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="one published GitHubDevMergeTagObservation"):
        _dev_merge_tag_topic(path)
