# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_pr_merged_projection reads the PR watcher's repo-merged events (OMN-19998).

The PR watcher publishes ``onex.evt.omnimarket.repo-merged.v1`` through
``clone-refresh publish`` for every merge it observes. These tests drive the
projection's runtime shim the way the projection host does (event payload plus
the injected ``_db``, ``_topic``, ``_event_type`` and ``_envelope_id`` keys) and
check that such an event becomes one ``pr_merged_events`` row, that a merge
announced by the git mirror as well stays one row, and that a non-merged state
writes nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.events.github import pr_merged_event_id
from omnimarket.events.topics import PR_MERGED_TOPIC_V1, REPO_MERGED_TOPIC_V1
from omnimarket.models.model_repo_merged import ModelRepoMerged
from omnimarket.nodes.node_canonical_clone_refresh_effect.handlers.handler_canonical_clone_refresh_serve import (
    load_canonical_clone_refresh_topics,
)
from omnimarket.nodes.node_canonical_clone_refresh_effect.models import (
    ModelRepoMerged as CloneRefreshRepoMerged,
)
from omnimarket.nodes.node_pr_merged_projection.handlers.handler_pr_merged_projection import (
    TABLE,
    HandlerPrMergedProjection,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

CONTRACT_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pr_merged_projection"
    / "contract.yaml"
)
REPO = "OmniNode-ai/omnimarket"
MERGE_SHA = "749f480bb02f5ce216ae161c4d7c99dd05738979"
PR_NUMBER = 4016

HANDLER = HandlerPrMergedProjection()


def _watcher_event(
    state: Literal["merged", "open", "closed"] = "merged",
) -> ModelRepoMerged:
    return ModelRepoMerged(
        repo=REPO,
        base="dev",
        merge_sha=MERGE_SHA,
        pr_number=PR_NUMBER,
        merged_at=datetime(2026, 10, 16, 15, 40, 12, tzinfo=UTC),
        source="watcher",
        state=state,
    )


def _dispatch(
    db: InmemoryDatabaseAdapter, topic: str, payload: dict[str, object]
) -> dict[str, object]:
    return HANDLER.handle(
        {
            **payload,
            "_db": db,
            "_topic": topic,
            "_event_type": topic,
            "_envelope_id": str(uuid4()),
        }
    )


@pytest.mark.unit
class TestRepoMergedFold:
    def test_watcher_merge_projects_one_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        out = _dispatch(
            db, REPO_MERGED_TOPIC_V1, _watcher_event().model_dump(mode="json")
        )
        assert out["rows_upserted"] == 1
        rows = db.query(TABLE)
        assert len(rows) == 1
        row = rows[0]
        assert row["event_id"] == str(pr_merged_event_id(REPO, PR_NUMBER, MERGE_SHA))
        assert row["repo"] == REPO
        assert row["pr_number"] == PR_NUMBER
        assert row["branch"] == f"refs/pull/{PR_NUMBER}/head"
        assert row["ticket"] == ""
        assert row["merged_at"] == "2026-10-16T15:40:12+00:00"

    def test_watcher_replay_stays_one_row(self) -> None:
        db = InmemoryDatabaseAdapter()
        payload = _watcher_event().model_dump(mode="json")
        _dispatch(db, REPO_MERGED_TOPIC_V1, payload)
        _dispatch(db, REPO_MERGED_TOPIC_V1, payload)
        assert len(db.query(TABLE)) == 1

    def test_same_merge_from_git_mirror_stays_one_row(self) -> None:
        """The git mirror names the same merge with the same shared event id."""
        db = InmemoryDatabaseAdapter()
        _dispatch(db, REPO_MERGED_TOPIC_V1, _watcher_event().model_dump(mode="json"))
        _dispatch(
            db,
            PR_MERGED_TOPIC_V1,
            {
                "event_id": str(pr_merged_event_id(REPO, PR_NUMBER, MERGE_SHA)),
                "repo": REPO,
                "branch": "omn-19998-construction-probe",
                "pr_number": PR_NUMBER,
                "ticket": "OMN-19998",
                "merged_at": "2026-10-16T15:40:12+00:00",
                "source": "mirror",
            },
        )
        assert len(db.query(TABLE)) == 1

    @pytest.mark.parametrize("state", ["open", "closed"])
    def test_not_merged_writes_nothing(self, state: Literal["open", "closed"]) -> None:
        db = InmemoryDatabaseAdapter()
        out = _dispatch(
            db, REPO_MERGED_TOPIC_V1, _watcher_event(state).model_dump(mode="json")
        )
        assert out["rows_upserted"] == 0
        assert out["skipped_reason"] == f"{REPO}#{PR_NUMBER} is {state}, not merged"
        assert db.query(TABLE) == []

    def test_pr_merged_topic_still_projects(self) -> None:
        db = InmemoryDatabaseAdapter()
        out = _dispatch(
            db,
            PR_MERGED_TOPIC_V1,
            {
                "event_id": "evt-actions",
                "topic": PR_MERGED_TOPIC_V1,
                "repo": REPO,
                "branch": "feature",
                "pr_number": 99,
                "ticket": "OMN-19998",
                "merged_at": "2026-10-16T12:00:00+00:00",
            },
        )
        assert out["rows_upserted"] == 1
        assert db.query(TABLE)[0]["branch"] == "feature"


@pytest.mark.unit
class TestRepoMergedWiring:
    def test_contract_subscribes_to_both_topics(self) -> None:
        contract = yaml.safe_load(CONTRACT_PATH.read_text())
        topics = contract["event_bus"]["subscribe_topics"]
        assert PR_MERGED_TOPIC_V1 in topics
        assert REPO_MERGED_TOPIC_V1 in topics
        produced = {e["topic"] for e in contract["externally_produced_topics"]}
        assert REPO_MERGED_TOPIC_V1 in produced

    def test_topic_is_the_one_clone_refresh_publishes(self) -> None:
        assert load_canonical_clone_refresh_topics().merged == REPO_MERGED_TOPIC_V1

    def test_clone_refresh_reexports_the_shared_model(self) -> None:
        assert CloneRefreshRepoMerged is ModelRepoMerged

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("repo", "someone-else/omnimarket"),
            ("repo", "not-a-slug"),
            ("merge_sha", "749F480BB"),
            ("merged_at", datetime(2026, 10, 16, 15, 40, 12)),
        ],
    )
    def test_clone_refresh_import_keeps_field_validation(
        self, field: str, value: object
    ) -> None:
        """Moving the model kept its validators: the old import path still refuses."""
        fields: dict[str, object] = {
            "repo": REPO,
            "base": "dev",
            "merge_sha": MERGE_SHA,
            "pr_number": PR_NUMBER,
            "merged_at": datetime(2026, 10, 16, 15, 40, 12, tzinfo=UTC),
            "source": "watcher",
        }
        CloneRefreshRepoMerged.model_validate(fields)
        fields[field] = value
        with pytest.raises(ValidationError):
            CloneRefreshRepoMerged.model_validate(fields)
