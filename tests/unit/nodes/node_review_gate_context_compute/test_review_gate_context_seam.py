# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams of the review verdict commit status (OMN-24614): the decision, the GitHub write node, and the branch protection audit.

Each seam fails when either side changes shape. (1) The decision's check name, state, title and summary reach the write node
as the exact request the write node plans to send to GitHub (the plan is the write node's own, dry-run, and is the same on
the in-memory bus and on the real broker: ``onex scm commit-status-create --dry-run``). (2) The name the decision carries is the
name the branch protection audit asks a repository's landing branch to require, so a rename on one side is red on the other.
(3) The command, completion and failure topics the three nodes declare are distinct, well formed and named for their node.
Three injected breaks each change one thing the seam depends on and the reading goes red.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml

from omnimarket.models.model_repo_write_io import ModelRepoWriteRequest
from omnimarket.models.model_review_gate_context import (
    ModelReviewGateContextRequest,
    ModelReviewGatePrSubject,
)
from omnimarket.nodes.node_branch_protection_audit_effect.models import (
    ModelBranchProtectionReviewVerdictRequirement,
)
from omnimarket.nodes.node_github_repo_write_effect.handlers.handler_github_repo_write import (
    planned_requests,
)
from omnimarket.nodes.node_review_gate_context_compute.handlers.handler_review_gate_context import (
    decide_review_gate_context,
)
from omnimarket.nodes.node_review_gate_context_compute.overlay import (
    ModelReviewGateContextOverlay,
)
from omnimarket.nodes.node_review_gate_context_effect.adapters.write_node_commit_status_poster import (
    WriteNodeCommitStatusPoster,
)
from omnimarket.nodes.node_review_gate_context_effect.overlay import (
    ModelReviewContextBusRoute,
)
from omnimarket.nodes.node_review_gate_context_effect.protocols.protocol_head_statuses import (
    ReviewContextPortError,
)
from tests.nodes.node_branch_protection_audit_effect.audit_fixtures import (
    GQL_RULES,
    ORG,
    REPO_SETTINGS,
    RULESET_MERGE_QUEUE_DISABLED,
    RULESET_RESTRICTING_MAIN,
    run_audit,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[4] / "src" / "omnimarket" / "nodes"
HEAD = "7265930bafc673e66ced5bb67b9464bc460f1d77"
REPO = "example-org/example-repo"
AUDIT_REPO = "omnibase_core"
CORRELATION = "0b0a0a0a-0000-4000-8000-000000000001"


def _policy() -> Any:
    document = yaml.safe_load(
        (NODES / "node_review_gate_context_compute" / "overlay.example.yaml").read_text(
            encoding="utf-8"
        )
    )
    return ModelReviewGateContextOverlay.model_validate(document).policy


def _decision() -> Any:
    subject = ModelReviewGatePrSubject(
        repository=REPO,
        pr_number=7,
        head_sha=HEAD,
        author_login="lane-author",
        head_ref="omn-1-change",
        title="OMN-1: a change",
    )
    return decide_review_gate_context(
        ModelReviewGateContextRequest(subject=subject, policy=_policy())
    )


def _write_request(
    name_override: str | None = None, correlation_id: str = CORRELATION
) -> ModelRepoWriteRequest:
    """The request the poster hands the write node for the decision, captured through a fake bus call."""
    seen: list[ModelRepoWriteRequest] = []

    def call(request: ModelRepoWriteRequest) -> tuple[bool, dict[str, Any]]:
        seen.append(request)
        return False, {"commit_status_id": 5}

    route = ModelReviewContextBusRoute(
        backend="bus",
        bus_lane="dev",
        workspace_env="EXAMPLE_WORKSPACE",
        timeout_seconds=30,
        requested_by_lane="review-gate-context",
    )
    decision = _decision()
    WriteNodeCommitStatusPoster(route, call).post(
        correlation_id=correlation_id,
        repository=decision.repository,
        head_sha=decision.head_sha,
        context=name_override or decision.check_name,
        state=decision.state.value,
        target_url=f"https://github.com/{decision.repository}/pull/{decision.pr_number}",
        description=decision.title,
    )
    return seen[0]


def test_the_decision_reaches_the_write_node_as_the_exact_github_request() -> None:
    decision = _decision()
    (http,) = planned_requests(_write_request())
    assert (http.method, http.path) == ("POST", f"/repos/{REPO}/statuses/{HEAD}")
    body = http.body
    assert body is not None
    assert body["context"] == decision.check_name == "fable-review-gate"
    assert body["state"] == "failure"
    assert body["target_url"] == f"https://github.com/{REPO}/pull/7"
    assert body["description"] == decision.title


def test_the_request_is_a_valid_write_node_command_for_the_sweeps_non_uuid_correlation() -> (
    None
):
    # The sweep names each post "<sweep id>:<owner/repo>#<number>": the write node's correlation id is a UUID.
    sweep_id = "sweep-0001:example-org/example-repo#7"
    request = _write_request(correlation_id=sweep_id)
    assert request.operation.value == "commit_status_create"
    assert isinstance(request.correlation_id, UUID)
    assert (
        _write_request(correlation_id=sweep_id).correlation_id == request.correlation_id
    )
    assert request.idempotency_key.startswith("review-ctx-7265930bafc6-failure-")


def test_break_a_renamed_context_is_refused_not_posted() -> None:
    with pytest.raises(ReviewContextPortError, match="refused before any call"):
        _write_request(name_override="review-verdict / renamed")


def _protection(contexts: list[str]) -> dict[str, object]:
    return {
        "required_status_checks": {
            "strict": False,
            "contexts": contexts,
            "checks": [{"context": c, "app_id": 15368} for c in contexts],
        },
        "enforce_admins": {"enabled": True},
    }


def _audit(contexts: list[str], context: str) -> Any:
    base = f"repos/{ORG}/{AUDIT_REPO}"
    rulesets = [RULESET_MERGE_QUEUE_DISABLED, RULESET_RESTRICTING_MAIN]
    fixtures: dict[str, object] = {
        "graphql": GQL_RULES,
        f"{base}/branches/main/protection": {
            "required_status_checks": {"strict": False, "contexts": [], "checks": []},
            "enforce_admins": {"enabled": True},
        },
        f"{base}/branches/dev/protection": _protection(contexts),
        base: REPO_SETTINGS,
        f"{base}/rulesets": rulesets,
    }
    for ruleset in rulesets:
        fixtures[f"{base}/rulesets/{ruleset['id']}"] = ruleset
    return run_audit(
        AUDIT_REPO,
        fixtures,
        branches="dev",
        policy_overrides={
            "review_verdict_context": context,
            "review_verdict_required": (
                ModelBranchProtectionReviewVerdictRequirement(
                    repo=AUDIT_REPO, branch="dev"
                ),
            ),
        },
    )


def test_the_name_the_decision_carries_is_the_name_the_audit_asks_for() -> None:
    name = _decision().check_name
    green = _audit(["CI Summary", name], name)
    assert green.exit_code == 0, green.report
    red = _audit(["CI Summary"], name)
    assert red.exit_code == 1
    assert "not found in required status checks" in red.report


def test_break_an_audit_that_asks_for_another_name_reads_red_on_a_protected_branch() -> (
    None
):
    name = _decision().check_name
    assert _audit(["CI Summary", name], name).exit_code == 0
    assert _audit(["CI Summary", name], "review-verdict / renamed").exit_code == 1


def _topics(node: str) -> dict[str, str]:
    contract = yaml.safe_load((NODES / node / "contract.yaml").read_text("utf-8"))
    return dict(contract["runtime_dispatch"]["terminal_events"]) | {
        "command": contract["runtime_dispatch"]["command_topic"]
    }


@pytest.mark.parametrize(
    ("node", "stem"),
    [
        ("node_review_gate_context_effect", "review-gate-context-post"),
        ("node_review_gate_context_orchestrator", "review-gate-context-sweep"),
    ],
)
def test_each_node_declares_its_own_well_formed_topics(node: str, stem: str) -> None:
    topics = _topics(node)
    assert topics["command"] == f"onex.cmd.omnimarket.{stem}-requested.v1"
    assert topics["success"] == f"onex.evt.omnimarket.{stem}-completed.v1"
    assert topics["failure"] == f"onex.evt.omnimarket.{stem}-failed.v1"


def test_the_nodes_topics_are_distinct() -> None:
    every = [
        t
        for node in (
            "node_review_gate_context_effect",
            "node_review_gate_context_orchestrator",
        )
        for t in _topics(node).values()
    ]
    assert len(every) == len(set(every))
