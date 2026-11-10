# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam test: the shapes of the GitHub write verbs (OMN-23559).

Every lane's handoff and the landing sweep read the request the ``onex scm``
write verbs send and the terminal the write node answers with. The two sides
are the CLI that builds ``ModelRepoWriteRequest`` and the node that answers
``ModelRepoWriteCompleted`` or ``ModelRepoWriteFailed``. This test fails when
either side changes shape: a field added, removed or renamed, a refusal reason
added or dropped, or a verb's body not reaching the node's planned request.

It runs the real CLI against the real node on the in-memory bus (the in-process
contract host of OMN-21279). The same commands were read on the real broker
(lab dev lane) from a lab host; that reading is in the pull request.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnimarket.github_landing.github_landing_requests import (
    edit_pull_request_request,
    pull_request_request,
)
from omnimarket.handlers.scm import scm_cli
from omnimarket.models.model_repo_write_io import (
    EnumRepoWriteOperation,
    EnumRepoWriteRefusal,
    ModelRepoWriteCompleted,
    ModelRepoWriteFailed,
    ModelRepoWriteRequest,
)
from tests.unit.nodes.node_github_repo_write_effect.test_handler_github_repo_write import (
    _HEAD as _NODE_HEAD,
)
from tests.unit.nodes.node_github_repo_write_effect.test_handler_github_repo_write import (
    _REPO,
    _handler,
    _ok,
    _pr,
    _Recorded,
    _req,
)

pytestmark = pytest.mark.unit

_REQUEST_FIELDS = frozenset(
    {
        "base",
        "body",
        "claim",
        "claims_dir",
        "conclusion",
        "correlation_id",
        "draft",
        "dry_run",
        "expected_head_sha",
        "head",
        "head_sha",
        "idempotency_key",
        "inputs",
        "lease",
        "name",
        "operation",
        "output",
        "pr_number",
        "ref",
        "repo",
        "requested_by_lane",
        "requested_by_run",
        "review_thread_id",
        "status",
        "thread_comment_id",
        "title",
        "workflow",
    }
)
_COMPLETED_FIELDS = frozenset(
    {
        "check_run_id",
        "comment_id",
        "correlation_id",
        "head_sha",
        "html_url",
        "http_statuses",
        "idempotency_key",
        "mode",
        "operation",
        "outcome",
        "pr_number",
        "quota",
        "replayed",
        "repo",
        "requested_by_lane",
        "requests",
        "review_id",
    }
)
_FAILED_FIELDS = frozenset(
    {
        "correlation_id",
        "detail",
        "http_status",
        "http_statuses",
        "idempotency_key",
        "mode",
        "operation",
        "outcome",
        "pr_number",
        "quota",
        "reason",
        "repo",
        "requested_by_lane",
        "requests",
        "retry_after_seconds",
        "terminal_failure_cause",
    }
)
_REFUSAL_REASONS = frozenset(
    {
        "claim_not_held",
        "contract_required",
        "credential_unavailable",
        "github_refused",
        "head_moved",
        "idempotency_key_conflict",
        "lease_expired",
        "lease_fence_stale",
        "lease_unreadable",
        "not_found",
        "quota_floor",
        "thread_not_replied",
        "transport_error",
    }
)
_HEAD = "a" * 40


def test_the_request_and_result_models_keep_the_shape_handoff_and_landing_read() -> (
    None
):
    assert frozenset(ModelRepoWriteRequest.model_fields) == _REQUEST_FIELDS
    assert frozenset(ModelRepoWriteCompleted.model_fields) == _COMPLETED_FIELDS
    assert frozenset(ModelRepoWriteFailed.model_fields) == _FAILED_FIELDS
    assert {r.value for r in EnumRepoWriteRefusal} == _REFUSAL_REASONS


def _pr_edit(*extra: str) -> dict[str, object]:
    result = CliRunner().invoke(
        scm_cli.scm_group,
        [
            "pr-edit",
            "OmniNode-ai/omnimarket#4000",
            "--head-sha",
            _HEAD,
            "--lane",
            "scm-seam-test",
            "--bus",
            "inmemory",
            "--timeout-s",
            "0.1",
            "--dry-run",
            *extra,
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    answer: dict[str, object] = json.loads(result.output)
    return answer


def _planned_patch_body(answer: dict[str, object]) -> dict[str, object]:
    result = answer["result"]
    assert isinstance(result, dict)
    completed = ModelRepoWriteCompleted.model_validate(result)
    patches = [r for r in completed.requests if r.method == "PATCH"]
    assert len(patches) == 1
    body = patches[0].body
    assert isinstance(body, dict)
    return body


def test_pr_edit_body_text_reaches_the_node_unchanged_on_the_in_memory_bus() -> None:
    answer = _pr_edit("--body", "SEAM-ALPHA given text")
    assert answer["outcome"] == "completed"
    assert _planned_patch_body(answer) == {"body": "SEAM-ALPHA given text"}


def test_pr_edit_body_file_reaches_the_node_unchanged_on_the_in_memory_bus(
    tmp_path: Path,
) -> None:
    body_file = tmp_path / "body.md"
    body_file.write_text("SEAM-BRAVO from a file\n", encoding="utf-8")
    answer = _pr_edit("--body-file", str(body_file))
    assert _planned_patch_body(answer) == {"body": "SEAM-BRAVO from a file\n"}


def test_two_requests_with_one_explicit_key_each_plan_their_own_body() -> None:
    first = _pr_edit("--body", "SEAM-ONE", "--idempotency-key", "seam-shared-key")
    second = _pr_edit("--body", "SEAM-TWO", "--idempotency-key", "seam-shared-key")
    assert _planned_patch_body(first) == {"body": "SEAM-ONE"}
    assert _planned_patch_body(second) == {"body": "SEAM-TWO"}
    first_result, second_result = first["result"], second["result"]
    assert isinstance(first_result, dict)
    assert isinstance(second_result, dict)
    assert first_result["idempotency_key"] == second_result["idempotency_key"]
    assert first_result["replayed"] is False
    assert second_result["replayed"] is False


def test_a_failure_terminal_validates_as_the_failed_model_with_a_known_reason() -> None:
    result = CliRunner().invoke(
        scm_cli.scm_group,
        [
            "pr-close",
            "OmniNode-ai/omnimarket#4000",
            "--head-sha",
            _HEAD,
            "--run",
            "seam-test",
            "--lane",
            "scm-seam-test",
            "--bus",
            "inmemory",
            "--timeout-s",
            "0.1",
            "--dry-run",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 1, result.output
    answer = json.loads(result.stdout)
    failed = ModelRepoWriteFailed.model_validate(answer["result"])
    assert failed.reason in set(EnumRepoWriteRefusal)
    assert failed.reason is EnumRepoWriteRefusal.CLAIM_NOT_HELD


async def test_a_reused_key_refusal_crosses_the_wire_as_the_failed_model() -> None:
    """The refusal a lane reads for a reused key is the typed terminal, same wire shape.

    The write node answers ``ModelRepoWriteFailed`` as JSON on its terminal
    topic and ``onex scm`` validates it back into the same model. This pins the
    new reason across that serialisation, with the first write recorded by the
    real handler and the second request carrying a different body.
    """
    transport = _Recorded(
        [
            (pull_request_request(_REPO, 4000), _ok(_pr())),
            (
                edit_pull_request_request(_REPO, 4000, {"body": "SEAM-FIRST"}),
                _ok(_pr()),
            ),
        ]
    )
    handler = _handler(transport)

    def edit(body: str, key: str) -> object:
        return handler.handle(
            _req(
                EnumRepoWriteOperation.PR_EDIT,
                pr_number=4000,
                expected_head_sha=_NODE_HEAD,
                body=body,
                idempotency_key=key,
            )
        )

    assert isinstance(
        await edit("SEAM-FIRST", "seam-wire-key"), ModelRepoWriteCompleted
    )

    refused = await edit("SEAM-SECOND", "seam-wire-key")

    assert isinstance(refused, ModelRepoWriteFailed)
    on_the_wire = json.loads(refused.model_dump_json())
    assert on_the_wire["outcome"] == "failed"
    assert on_the_wire["reason"] == "idempotency_key_conflict"
    assert on_the_wire["requests"] == []
    assert "SEAM-FIRST" not in json.dumps(on_the_wire)
    assert "SEAM-SECOND" not in json.dumps(on_the_wire)
    read_back = ModelRepoWriteFailed.model_validate(on_the_wire)
    assert read_back.reason is EnumRepoWriteRefusal.IDEMPOTENCY_KEY_CONFLICT
