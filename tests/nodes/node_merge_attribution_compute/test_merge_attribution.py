# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Historical readers must distinguish a host identity from an engine."""

from omnimarket.nodes.node_merge_attribution_compute import HandlerMergeAttribution
from omnimarket.nodes.node_merge_attribution_compute.models.model_merge_attribution import (
    ModelMergeAttributionRequest,
)


def read(**over):
    payload = {"pr": "repo#7", "merged_at": "2026-10-25T12:00:00Z", **over}
    return HandlerMergeAttribution().handle(ModelMergeAttributionRequest(**payload))


def test_host_commit_identity_is_never_an_engine():
    r = read(author="worker-h101 <host@example.test>", host_identity_prefix="worker-")
    assert r.host == "h101"
    assert r.engine is None
    assert r.reason == "no_terminal"


def test_own_worker_merged_result_reads_actual_engine_from_cost():
    r = read(
        workers=[
            {
                "lease_id": 7,
                "subject": "owner/repo#7",
                "released_at": "2026-10-25T11:00:00Z",
                "engine": "codex_high",
                "host": "h101",
                "result": "merged",
            }
        ]
    )
    assert (r.engine, r.host, r.reason) == ("codex", "h101", "landing_worker")


def test_failed_worker_and_post_merge_worker_are_not_merge_evidence():
    for kind, when in [
        ("blocked", "2026-10-25T11:00:00Z"),
        ("merged", "2026-10-25T13:00:00Z"),
    ]:
        r = read(
            workers=[
                {
                    "lease_id": 7,
                    "subject": "repo#7",
                    "released_at": when,
                    "engine": "codex",
                    "host": "h101",
                    "result": kind,
                }
            ]
        )
        assert r.engine is None


def test_terminal_engine_is_attempt_specific_not_first_lane_model():
    rows = [
        {
            "ts": "2026-10-25T09:00:00Z",
            "kind": "CLAIM",
            "cells": {"lane": "l", "model": "sonnet"},
        },
        {
            "ts": "2026-10-25T10:00:00Z",
            "kind": "TERMINAL",
            "cells": {"lane": "l", "pr": "repo#7", "engine": "codex", "host": "h101"},
        },
    ]
    r = read(rows=rows)
    assert (r.engine, r.host) == ("codex", "h101")


def test_terminal_with_missing_lane_model_is_distinct():
    r = read(
        rows=[
            {
                "ts": "2026-10-25T10:00:00Z",
                "kind": "TERMINAL",
                "cells": {"lane": "l", "pr": "repo#7", "model": "none"},
            }
        ]
    )
    assert r.reason == "missing_model"
    assert r.engine is None


def test_explicit_automation_and_person_are_provenance_not_invented_engines():
    r = read(
        author="dependabot[bot] <bot@example.test>", message="Onex-Workflow: Release\n"
    )
    assert (r.reason, r.engine) == ("automation", None)
    r = read(author="A Person <person@example.test>")
    assert (r.reason, r.engine) == ("person", None)


def test_model_trailer_recovers_engine_without_guessing_from_prose():
    assert (
        read(message="Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>").engine
        == "claude-opus"
    )
    assert read(message="This fixes a Codex issue").engine is None


def test_future_claim_and_other_pr_do_not_supply_an_engine():
    r = read(
        rows=[
            {
                "ts": "2026-10-25T13:00:00Z",
                "kind": "CLAIM",
                "cells": {"lane": "l", "model": "opus"},
            },
            {
                "ts": "2026-10-25T11:00:00Z",
                "kind": "TERMINAL",
                "cells": {"lane": "l", "pr": "repo#8", "model": "codex"},
            },
        ]
    )
    assert r.engine is None


def test_diagnosis_and_failed_terminal_do_not_claim_a_pr_they_only_mention():
    for cells in [
        {"worktree": "none", "outcome": "done", "model": "opus"},
        {"pr": "repo#7", "outcome": "skipped-owned", "model": "opus"},
        {"pr": "repo#7", "outcome": "blocked", "model": "opus"},
    ]:
        r = read(
            rows=[
                {
                    "ts": "2026-10-25T11:00:00Z",
                    "kind": "TERMINAL",
                    "cells": cells,
                    "text": "Looked at repo#7; no changes.",
                }
            ]
        )
        assert r.engine is None


def test_another_prs_release_or_blocker_is_not_authorship():
    for scope in ["repo#8", "none:diagnosis-only"]:
        r = read(
            rows=[
                {
                    "ts": "2026-10-25T11:00:00Z",
                    "kind": "TERMINAL",
                    "cells": {
                        "pr": scope,
                        "worktree": "kept:worktree",
                        "outcome": "done",
                        "model": "opus",
                    },
                    "text": "Opened repo#8; it waits on release repo#7.",
                }
            ]
        )
        assert r.engine is None


def test_cause_worker_cost_names_cause_but_result_names_new_source_pr():
    r = read(
        workers=[
            {
                "lease_id": 7,
                "subject": "cause:owner/repo:group",
                "fix_ref": "owner/repo#7",
                "released_at": "2026-10-25T11:00:00Z",
                "engine": "claude_opus",
                "host": "h101",
                "result": "cause_fix_submitted",
            }
        ]
    )
    assert (r.engine, r.host, r.reason) == ("claude-opus", "h101", "landing_worker")
