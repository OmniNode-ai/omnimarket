# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Candidates from a schema-1 PR watcher state file (OMN-20422).

The pre-registration takes PR creation time and head from the PR watcher
snapshot, never from the GitHub API. Open and merged PRs are candidates; bodies
are not read into the candidate.
"""

from __future__ import annotations

import json
from pathlib import Path

from omnimarket.nodes.node_shadow_review_effect.models.model_shadow_review import (
    ModelShadowReviewCandidate,
)

# A PR that merged before the runner first saw it is still a PR created in the
# window; leaving it out would bias the sample toward slow PRs. Its head may be
# gone after merge, which the diff stage drops as head-unavailable, counted.
_SAMPLED_STATES = frozenset({"OPEN", "MERGED"})


def candidates_from_watcher_state(path: Path) -> tuple[ModelShadowReviewCandidate, ...]:
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema") != 1:
        raise ValueError(f"unsupported watcher state schema: {state.get('schema')!r}")
    out: list[ModelShadowReviewCandidate] = []
    for key, pr in state.get("prs", {}).items():
        facts = pr.get("facts") or {}
        if facts.get("state") not in _SAMPLED_STATES:
            continue
        if not (
            facts.get("created_at") and facts.get("head_sha") and facts.get("head_ref")
        ):
            continue
        candidate = ModelShadowReviewCandidate(
            repo=str(facts["repo"]),
            number=int(facts["number"]),
            created_at=str(facts["created_at"]),
            head_sha=str(facts["head_sha"]),
            head_ref=str(facts["head_ref"]),
            base=str(facts.get("base") or "main"),
            title=str(facts.get("title") or "")[:200],
            author=str(facts.get("author") or ""),
            author_is_bot=bool(facts.get("author_is_bot")),
            draft=bool(facts.get("draft")),
            evidence_companion=(
                str(facts["evidence_companion"])
                if facts.get("evidence_companion")
                else None
            ),
            watcher_class=str(pr.get("cls") or ""),
        )
        if candidate.key != key:
            raise ValueError(f"watcher key {key!r} disagrees with its facts")
        out.append(candidate)
    return tuple(out)
