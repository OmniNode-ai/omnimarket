# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""No code path merges outside the merge queue (OMN-19929, plan task S4).

Every omnimarket path that can land a pull request is pinned here against a
recorded GitHub adapter: on a branch with a merge queue it either enqueues or
refuses, and its call log never carries a direct merge (``gh pr merge``, the
REST ``PUT pulls/<n>/merge`` or the ``mergePullRequest`` mutation).

The paths, from the plan's inventory:

* ``node_pr_landing_github_effect`` -- the landing path; enqueue is its only
  merge verb and an arm on a queue branch is refused;
* ``node_pr_lifecycle_merge_effect`` -- arms auto-merge and enqueues through
  ``GitHubMergeQueueAdapter``, never a direct merge;
* ``node_auto_merge_effect`` -- the legacy ``gh pr merge`` surface; on a queue
  branch, or when the branch rules cannot be read, it refuses;
* ``HandlerAdminMerge`` -- the admin merge of a PR the queue had refused, which
  is the out-of-queue merge itself; it is removed, and this file keeps it gone.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import pytest

from omnimarket.events.pr_landing_github.enum_pr_landing_github_operation import (
    EnumPrLandingGithubOperation,
)
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
)
from omnimarket.nodes.node_auto_merge_effect.handlers.handler_auto_merge_effect import (
    HandlerAutoMergeEffect,
)
from omnimarket.nodes.node_auto_merge_effect.models.model_auto_merge_input import (
    ModelAutoMergeInput,
)
from omnimarket.nodes.node_pr_landing_github_effect.handlers import (
    HandlerPrLandingGithubEffect,
)
from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubMode,
    ModelPrLandingGithubRequest,
)
from omnimarket.nodes.node_pr_lifecycle_merge_effect.handlers.adapter_github_merge_queue import (
    GitHubMergeQueueAdapter,
)
from tests.unit.nodes.node_pr_landing_github_effect.fake_transport import (
    FakeGithubLandingTransport,
    load_scenario,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src" / "omnimarket"
_CORRELATION = UUID("00000000-0000-4000-8000-000000019929")
_QUEUE_REPO = "OmniNode-ai/omnibase_infra"
_LEGACY_ARM_ENV_VAR = "OMNIMARKET_LEGACY_MERGE_ARM_ENABLED"


def _is_direct_merge_argv(argv: list[str]) -> bool:
    joined = " ".join(argv)
    if argv[:3] == ["gh", "pr", "merge"]:
        return True
    return "mergePullRequest" in joined or "/merge" in joined


def _is_direct_merge_request(request: ModelGithubHttpRequest) -> bool:
    if request.method == "PUT" and request.path.rstrip("/").endswith("/merge"):
        return True
    query = (request.body or {}).get("query")
    return isinstance(query, str) and "mergePullRequest" in query


# --- HandlerAdminMerge is gone ----------------------------------------------


@pytest.mark.unit
def test_no_admin_merge_handler_remains_in_source() -> None:
    offenders = sorted(
        str(path.relative_to(_REPO_ROOT))
        for path in _SRC.rglob("*.py")
        if "HandlerAdminMerge" in path.read_text(encoding="utf-8")
        or "admin_merge" in path.name
    )
    assert offenders == []


@pytest.mark.unit
def test_orchestrator_start_command_carries_no_admin_merge_switch() -> None:
    from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
        ModelPrLifecycleStartCommand,
    )

    fields = set(ModelPrLifecycleStartCommand.model_fields)
    assert "enable_admin_merge_fallback" not in fields
    assert "admin_fallback_threshold_minutes" not in fields


# --- node_pr_landing_github_effect ------------------------------------------


@pytest.mark.unit
def test_landing_effect_has_no_direct_merge_operation() -> None:
    names = {op.value for op in EnumPrLandingGithubOperation}
    assert not any(n == "merge" or n.startswith("merge_") for n in names)
    assert "enqueue" in names


@pytest.mark.unit
@pytest.mark.parametrize(
    ("scenario", "expect_mutation"),
    [
        ("enqueue_200", True),
        ("arm_auto_merge_refused_merge_queue", False),
    ],
)
async def test_landing_effect_on_a_queue_branch_enqueues_or_refuses(
    scenario: str, expect_mutation: bool
) -> None:
    recorded = load_scenario(scenario)
    command = ModelPrLandingGithubRequest.model_validate(
        {
            **recorded.command,
            "correlation_id": _CORRELATION,
            "operation": recorded.operation,
            "mode": EnumPrLandingGithubMode.ENFORCE,
        }
    )
    transport = FakeGithubLandingTransport.for_scenario(scenario)
    await HandlerPrLandingGithubEffect(transport).handle(command)
    assert len(transport.sent) == (2 if expect_mutation else 1)
    assert not any(_is_direct_merge_request(r) for r in transport.sent)


# --- node_pr_lifecycle_merge_effect -----------------------------------------


class _RecordingMergeQueueAdapter(GitHubMergeQueueAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[str]] = []

    async def _run(
        self,
        argv: list[str],
        *,
        context: str,
        check: bool = True,
        timeout_s: float = 30.0,
    ) -> tuple[int, str, str]:
        self.calls.append(argv)
        joined = " ".join(argv)
        if argv[:3] == ["gh", "pr", "view"]:
            return 0, json.dumps({"id": "PR_node"}), ""
        if "enqueuePullRequest" in joined:
            payload = {
                "data": {"enqueuePullRequest": {"mergeQueueEntry": {"position": 1}}}
            }
            return 0, json.dumps(payload), ""
        return 0, "{}", ""


@pytest.mark.unit
@pytest.mark.parametrize("use_merge_queue", [True, False])
async def test_lifecycle_merge_adapter_never_merges_directly(
    use_merge_queue: bool,
) -> None:
    adapter = _RecordingMergeQueueAdapter()
    await adapter.merge_pr(_QUEUE_REPO, 4197, use_merge_queue=use_merge_queue)
    assert adapter.calls
    assert not any(_is_direct_merge_argv(argv) for argv in adapter.calls)
    enqueued = any("enqueuePullRequest" in " ".join(a) for a in adapter.calls)
    assert enqueued is use_merge_queue


# --- node_auto_merge_effect -------------------------------------------------


def _auto_merge_run(
    rules: tuple[int, str],
) -> tuple[list[list[str]], Callable[[list[str]], tuple[int, str, str]]]:
    calls: list[list[str]] = []

    def run(argv: list[str]) -> tuple[int, str, str]:
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "view"]:
            fields = argv[-1]
            if "mergeStateStatus" in fields:
                return 0, json.dumps({"mergeStateStatus": "CLEAN"}), ""
            if "baseRefName" in fields:
                return 0, json.dumps({"baseRefName": "dev"}), ""
            if "mergeCommit" in fields:
                return 0, json.dumps({"mergeCommit": {"oid": "a" * 40}}), ""
            return 0, json.dumps({"reviewDecision": "APPROVED"}), ""
        if argv[:2] == ["gh", "api"] and "/rules/branches/" in argv[2]:
            rc, body = rules
            return rc, body, "" if rc == 0 else "HTTP 403"
        return 0, "", ""

    return calls, run


_QUEUE_RULES = (0, json.dumps([{"type": "merge_queue", "parameters": {}}]))
_NO_QUEUE_RULES = (0, json.dumps([{"type": "pull_request", "parameters": {}}]))
_UNREADABLE_RULES = (1, "")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("rules", "expect_merge"),
    [
        (_QUEUE_RULES, False),
        (_UNREADABLE_RULES, False),
        (_NO_QUEUE_RULES, True),
    ],
    ids=["queue-branch-refused", "unreadable-rules-refused", "no-queue-merges"],
)
async def test_auto_merge_effect_refuses_a_queue_branch(
    monkeypatch: pytest.MonkeyPatch,
    rules: tuple[int, str],
    expect_merge: bool,
) -> None:
    monkeypatch.setenv(_LEGACY_ARM_ENV_VAR, "true")
    calls, run = _auto_merge_run(rules)
    handler = HandlerAutoMergeEffect(run_fn=run, sleep_fn=lambda _s: None)
    result = await handler.handle(
        ModelAutoMergeInput(
            correlation_id=_CORRELATION,
            pr_number=4197,
            repo=_QUEUE_REPO,
            ticket_id="OMN-19929",
        )
    )
    merged_calls = [argv for argv in calls if _is_direct_merge_argv(argv)]
    assert result.merged is expect_merge
    assert bool(merged_calls) is expect_merge
    if not expect_merge:
        assert result.blocked_reason is not None
        assert "merge queue" in result.blocked_reason
