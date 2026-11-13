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
from click.testing import CliRunner, Result

from omnimarket.github_landing.github_landing_requests import (
    edit_pull_request_request,
    pull_request_request,
)
from omnimarket.handlers.scm import scm_cli
from omnimarket.models.model_repo_write_io import (
    EnumRepoWriteMode,
    EnumRepoWriteOperation,
    EnumRepoWriteRefusal,
    ModelRepoWriteCompleted,
    ModelRepoWriteFailed,
    ModelRepoWriteRequest,
)
from omnimarket.nodes.node_github_repo_write_effect.overlay import NODE_NAME
from omnimarket.nodes.node_github_repo_write_effect.services.repo_variable import (
    get_variable_request,
)
from omnimarket.nodes.node_github_repo_write_effect.services.runner_registration import (
    list_runners_request,
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
        "add_labels",
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
        "remove_labels",
        "replaces",
        "repo",
        "requested_by_lane",
        "requested_by_run",
        "review_thread_id",
        "runner_name",
        "status",
        "thread_comment_id",
        "title",
        "variable_name",
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
        "runner_name",
        "runner_removed",
        "runners",
        "variable_digest",
        "variable_in_sync",
        "variable_name",
        "variable_value",
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
        "label_not_allowed",
        "lease_expired",
        "lease_fence_stale",
        "lease_unreadable",
        "not_found",
        "quota_floor",
        "runner_not_allowed",
        "runner_online",
        "secret_refused",
        "thread_not_replied",
        "transport_error",
        "variable_not_allowed",
        "variable_source_unreadable",
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


_BUNDLE_REPO = "OmniNode-ai/example_repo"
_REPLACED = f"{_BUNDLE_REPO}#1574"


def _pr_create_request(**extra: object) -> ModelRepoWriteRequest:
    return ModelRepoWriteRequest.model_validate(
        {
            "correlation_id": "00000000-0000-4000-8000-000000000001",
            "operation": "pr_create",
            "repo": _BUNDLE_REPO,
            "idempotency_key": "seam-replaces-key",
            "requested_by_lane": "scm-seam-test",
            "head": "omn-19998-glm-lane-route",
            "base": "main",
            "title": "feat(OMN-19998): GLM lane route",
            **extra,
        }
    )


def test_pr_create_carries_replaces_as_a_list_of_pull_request_refs_the_handoff_reads() -> (
    None
):
    """The shape handoff and landing read: ``replaces`` is a list of OWNER/REPO#N, on pr_create only."""
    request = _pr_create_request(replaces=[_REPLACED])

    wire = json.loads(request.model_dump_json())
    assert wire["replaces"] == [_REPLACED]
    assert ModelRepoWriteRequest.model_validate(wire).replaces == (_REPLACED,)


def test_a_pr_create_without_replaces_leaves_the_field_out_of_the_envelope() -> None:
    """A runtime that predates the field forbids extras: the envelope of a request that names none must not carry it."""
    wire = json.loads(_pr_create_request().model_dump_json())
    assert "replaces" not in wire


@pytest.mark.parametrize(
    "bad",
    ["1574", "example_repo#1574", f"{_BUNDLE_REPO}#0", f"{_BUNDLE_REPO}#x"],
)
def test_a_replaces_entry_that_is_not_a_pull_request_ref_is_refused_by_the_model(
    bad: str,
) -> None:
    with pytest.raises(ValueError, match="OWNER/REPO#N"):
        _pr_create_request(replaces=[bad])


def test_replaces_is_read_only_by_pr_create() -> None:
    with pytest.raises(ValueError, match="read only by pr_create"):
        ModelRepoWriteRequest.model_validate(
            {
                "correlation_id": "00000000-0000-4000-8000-000000000002",
                "operation": "pr_comment",
                "repo": _BUNDLE_REPO,
                "idempotency_key": "seam-replaces-key",
                "requested_by_lane": "scm-seam-test",
                "pr_number": 4000,
                "body": "text",
                "replaces": [_REPLACED],
            }
        )


def _invoke_pr_create(
    monkeypatch: pytest.MonkeyPatch, *extra: str
) -> tuple[dict[str, object], list[tuple[str, ...]]]:
    """Run the real CLI's pr-create to the node on the in-memory bus; the guard is recorded and let through."""
    asked: list[tuple[str, ...]] = []

    def let_through(**kwargs: object) -> None:
        replaces = kwargs["replaces"]
        assert isinstance(replaces, tuple)
        asked.append(replaces)
        return

    monkeypatch.setattr(scm_cli, "refuse_or_continue", let_through)
    result = CliRunner().invoke(
        scm_cli.scm_group,
        [
            "pr-create",
            _BUNDLE_REPO,
            "--head",
            "omn-19998-glm-lane-route",
            "--base",
            "main",
            "--title",
            "feat(OMN-19998): GLM lane route",
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
    answer: dict[str, object] = json.loads(result.stdout.splitlines()[-1])
    return answer, asked


def test_pr_create_replaces_flag_reaches_the_guard_and_the_node_and_keys_the_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI hands ``--replaces`` (repeatable) to the guard and to the node's request, and it changes the write's key."""
    plain, plain_asked = _invoke_pr_create(monkeypatch)
    given, given_asked = _invoke_pr_create(
        monkeypatch, "--replaces", _REPLACED, "--replaces", f"{_BUNDLE_REPO}#1575"
    )

    assert plain_asked == [()]
    assert given_asked == [(_REPLACED, f"{_BUNDLE_REPO}#1575")]
    plain_result, given_result = plain["result"], given["result"]
    assert isinstance(plain_result, dict)
    assert isinstance(given_result, dict)
    assert plain_result["idempotency_key"] != given_result["idempotency_key"]


# --- the repository variable verbs (OMN-19998) ----------------------------------

# Test-local variable name and value only; no real variable name or value
# lives in src, the contract or the shipped overlay example.
_WRITE_REPO = "OmniNode-ai/omnimarket"
_VAR_ONE = "LAB_VAR_ONE"
_VAR_TWO = "LAB_VAR_TWO"
_VAR_VALUE = "lab-one-value"
_VAR_SOURCE = "lab/vars/one.txt"


def _pin_variable_overlay(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The write node's overlay and the declared value source under one pinned root.

    The node reads both lazily out of ``ONEX_SKILL_OVERLAY_ROOTS`` when a
    repo_variable request arrives: the overlay allowlists one variable for this
    repository, and the value lives only in the file the entry declares, never
    on the command line.
    """
    overlay = tmp_path / NODE_NAME / "overlay.yaml"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(
        "pr_label:\n"
        "  allowed_labels:\n"
        "    - lab:ready\n"
        "repo_variables:\n"
        "  entries:\n"
        f"    - repository: {_WRITE_REPO}\n"
        f"      name: {_VAR_ONE}\n"
        f"      source: {_VAR_SOURCE}\n"
        "  refuse_name_markers:\n"
        "    - TOKEN\n"
        "  refuse_value_markers:\n"
        "    - LABSECRET\n",
        encoding="utf-8",
    )
    source = tmp_path / _VAR_SOURCE
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(f"{_VAR_VALUE}\n", encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))


def _variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, verb: str, name: str
) -> Result:
    """Run one variable verb through the real CLI to the real node on the in-memory bus."""
    _pin_variable_overlay(monkeypatch, tmp_path)
    return CliRunner().invoke(
        scm_cli.scm_group,
        [
            verb,
            _WRITE_REPO,
            "--variable",
            name,
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


def test_variable_set_dry_run_plans_exactly_one_read_of_the_allowed_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The value never crosses the seam: the plan is one GET of the allowlisted name."""
    result = _variable(monkeypatch, tmp_path, "variable-set", _VAR_ONE)
    assert result.exit_code == 0, result.output
    answer = json.loads(result.output)
    assert answer["outcome"] == "completed"
    completed = ModelRepoWriteCompleted.model_validate(answer["result"])
    assert completed.mode is EnumRepoWriteMode.DRY_RUN
    assert completed.requests == (get_variable_request(_WRITE_REPO, _VAR_ONE),)
    (read,) = completed.requests
    assert read.method == "GET"
    assert read.path == f"/repos/{_WRITE_REPO}/actions/variables/{_VAR_ONE}"
    assert _VAR_VALUE not in result.output


def test_variable_set_of_a_name_the_overlay_does_not_allow_fails_without_a_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A name outside the overlay answers the typed failed terminal, exit one."""
    result = _variable(monkeypatch, tmp_path, "variable-set", _VAR_TWO)
    assert result.exit_code == scm_cli.EXIT_FAILED, result.output
    answer = json.loads(result.output)
    assert answer["outcome"] == "failed"
    failed = ModelRepoWriteFailed.model_validate(answer["result"])
    assert failed.reason is EnumRepoWriteRefusal.VARIABLE_NOT_ALLOWED
    assert failed.requests == ()


def test_variable_get_dry_run_plans_the_same_single_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = _variable(monkeypatch, tmp_path, "variable-get", _VAR_ONE)
    assert result.exit_code == 0, result.output
    answer = json.loads(result.output)
    assert answer["outcome"] == "completed"
    completed = ModelRepoWriteCompleted.model_validate(answer["result"])
    assert completed.requests == (get_variable_request(_WRITE_REPO, _VAR_ONE),)


# --- the runner verbs (OMN-24274) ------------------------------------------------

# Test-local organization and runner names only; the real names live in the
# overlays repository, never in src, the contract or the shipped overlay example.
_RUNNER_ORG = "example-org"
_RUNNER_REPO = f"{_RUNNER_ORG}/example-repo"
_RUNNER_ONE = "example-runner-1"
_RUNNER_OFF_LIMITS = "example-runner-off-limits"


def _pin_runner_overlay(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The write node's overlay, with one allowed runner, under one pinned root."""
    overlay = tmp_path / NODE_NAME / "overlay.yaml"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(
        "pr_label:\n"
        "  allowed_labels:\n"
        "    - lab:ready\n"
        "runner_removals:\n"
        "  entries:\n"
        f"    - organization: {_RUNNER_ORG}\n"
        f"      name: {_RUNNER_ONE}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))


def _runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, verb: str, *extra: str
) -> Result:
    """Run one runner verb through the real CLI to the real node on the in-memory bus."""
    _pin_runner_overlay(monkeypatch, tmp_path)
    return CliRunner().invoke(
        scm_cli.scm_group,
        [
            verb,
            _RUNNER_REPO,
            *extra,
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


def test_runner_list_dry_run_plans_the_first_page_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = _runner(monkeypatch, tmp_path, "runner-list")
    assert result.exit_code == 0, result.output
    answer = json.loads(result.output)
    assert answer["outcome"] == "completed"
    completed = ModelRepoWriteCompleted.model_validate(answer["result"])
    assert completed.mode is EnumRepoWriteMode.DRY_RUN
    assert completed.requests == (list_runners_request(_RUNNER_ORG, 1),)
    (read,) = completed.requests
    assert read.method == "GET"
    assert read.path == f"/orgs/{_RUNNER_ORG}/actions/runners?per_page=100&page=1"


def test_runner_remove_dry_run_of_an_allowed_runner_plans_only_the_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = _runner(monkeypatch, tmp_path, "runner-remove", "--runner", _RUNNER_ONE)
    assert result.exit_code == 0, result.output
    completed = ModelRepoWriteCompleted.model_validate(
        json.loads(result.output)["result"]
    )
    assert completed.requests == (list_runners_request(_RUNNER_ORG, 1),)


def test_runner_remove_of_a_name_the_overlay_does_not_allow_fails_without_a_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = _runner(
        monkeypatch, tmp_path, "runner-remove", "--runner", _RUNNER_OFF_LIMITS
    )
    assert result.exit_code == scm_cli.EXIT_FAILED, result.output
    answer = json.loads(result.output)
    assert answer["outcome"] == "failed"
    failed = ModelRepoWriteFailed.model_validate(answer["result"])
    assert failed.reason is EnumRepoWriteRefusal.RUNNER_NOT_ALLOWED
    assert failed.requests == ()
