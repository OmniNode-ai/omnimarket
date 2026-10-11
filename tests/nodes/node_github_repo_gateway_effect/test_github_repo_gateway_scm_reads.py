# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gateway's OMN-20912 reads on a recorded fake transport.

workflow_runs, run_jobs, job_log_tail, artifacts, pr_text and releases each
send exactly the recorded requests (strict, ordered replay), page inside the
node, honour their bounds, and return their own typed result.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import io
import urllib.request

import pytest
from pydantic import BaseModel, ValidationError

from omnimarket.github_landing.github_landing_requests import (
    GithubLandingRequestError,
    next_page_request,
)
from omnimarket.github_landing.github_landing_transport import (
    _AuthStrippingRedirectHandler,
    _read_capped,
)
from omnimarket.nodes.node_github_repo_gateway_effect.dispatcher import dispatch
from omnimarket.nodes.node_github_repo_gateway_effect.models.model_gateway_io import (
    MAX_TEXT_CHARS,
    EnumArtifactFetchRefusal,
    EnumGithubGatewayOperation,
    ModelArtifactsResult,
    ModelGithubGatewayRequest,
    ModelGithubGatewayResponse,
    ModelJobLogTailResult,
    ModelPrTextResult,
    ModelReleasesResult,
    ModelRunJobsResult,
    ModelWorkflowRunsResult,
)
from omnimarket.nodes.node_merge_sweep_compute.protocols import GitHubTransportError
from tests.nodes.node_github_repo_gateway_effect.recorded_transport import (
    HEAD,
    REPO,
    BytesExchange,
    JsonExchange,
    RecordedGatewayTransport,
    artifact,
    get,
    issue_comment,
    json_response,
    pull_request,
    release,
    run_job,
    tag,
    workflow_run,
)

pytestmark = pytest.mark.unit

_RUN = 30433642
_BASE = f"/repos/{REPO}"

# The six OMN-20912 operations and the typed result each must return; the
# protocol tests' every-operation coverage check unions this map with its own.
SCM_READ_RESULT_MODELS: dict[EnumGithubGatewayOperation, type[BaseModel]] = {
    EnumGithubGatewayOperation.WORKFLOW_RUNS: ModelWorkflowRunsResult,
    EnumGithubGatewayOperation.RUN_JOBS: ModelRunJobsResult,
    EnumGithubGatewayOperation.JOB_LOG_TAIL: ModelJobLogTailResult,
    EnumGithubGatewayOperation.ARTIFACTS: ModelArtifactsResult,
    EnumGithubGatewayOperation.PR_TEXT: ModelPrTextResult,
    EnumGithubGatewayOperation.RELEASES: ModelReleasesResult,
}


def _run(
    request: ModelGithubGatewayRequest,
    exchanges: list[JsonExchange | BytesExchange],
) -> tuple[BaseModel, RecordedGatewayTransport]:
    transport = RecordedGatewayTransport(exchanges)
    result = dispatch(request, transport)
    transport.assert_drained()
    return result, transport


def _req(op: EnumGithubGatewayOperation, **kw: object) -> ModelGithubGatewayRequest:
    return ModelGithubGatewayRequest.model_validate(
        {"operation": op, "repo": REPO, **kw}
    )


# --- workflow_runs -------------------------------------------------------------


def test_workflow_runs_pages_until_the_limit_and_says_it_was_cut() -> None:
    page_1 = [workflow_run(1000 + i) for i in range(100)]
    page_2 = [workflow_run(2000 + i) for i in range(60)]
    first = f"{_BASE}/actions/workflows/ci.yml/runs?branch=dev&per_page=100"
    second = f"{first}&page=2"
    result, transport = _run(
        _req(
            EnumGithubGatewayOperation.WORKFLOW_RUNS,
            workflow="ci.yml",
            branch="dev",
            limit=150,
        ),
        [
            JsonExchange(
                get(first),
                json_response(
                    {"total_count": 412, "workflow_runs": page_1}, next_path=second
                ),
            ),
            JsonExchange(
                get(second),
                json_response(
                    {"total_count": 412, "workflow_runs": page_2},
                    next_path=f"{first}&page=3",
                ),
            ),
        ],
    )
    assert isinstance(result, ModelWorkflowRunsResult)
    assert len(result.runs) == 150
    assert result.truncated is True
    assert result.total_count == 412
    assert result.runs[0].id == 1000
    assert result.runs[-1].id == 2049
    assert len(transport.sent) == 2


def test_workflow_runs_one_short_page_is_complete() -> None:
    path = f"{_BASE}/actions/runs?head_sha={HEAD}&event=push&status=failure&per_page=30"
    result, _ = _run(
        _req(
            EnumGithubGatewayOperation.WORKFLOW_RUNS,
            head_sha=HEAD,
            event="push",
            status="failure",
        ),
        [
            JsonExchange(
                get(path),
                json_response(
                    {
                        "total_count": 1,
                        "workflow_runs": [workflow_run(5, conclusion="failure")],
                    }
                ),
            )
        ],
    )
    assert isinstance(result, ModelWorkflowRunsResult)
    assert result.truncated is False
    assert [r.conclusion for r in result.runs] == ["failure"]
    # fields GitHub sends that the model does not declare are dropped
    assert "actor" not in result.runs[0].model_dump()


def test_workflow_runs_error_status_raises_with_the_message() -> None:
    path = f"{_BASE}/actions/workflows/missing.yml/runs?per_page=30"
    with pytest.raises(GitHubTransportError, match="HTTP 404: Not Found"):
        _run(
            _req(EnumGithubGatewayOperation.WORKFLOW_RUNS, workflow="missing.yml"),
            [
                JsonExchange(
                    get(path), json_response({"message": "Not Found"}, status=404)
                )
            ],
        )


# --- run_jobs --------------------------------------------------------------------


def test_run_jobs_returns_every_attempt_with_steps() -> None:
    path = f"{_BASE}/actions/runs/{_RUN}/jobs?filter=all&per_page=100&page=1"
    jobs = [
        run_job(1, attempt=1, conclusion="failure"),
        run_job(2, attempt=2, conclusion="success"),
    ]
    result, _ = _run(
        _req(EnumGithubGatewayOperation.RUN_JOBS, run_id=_RUN),
        [JsonExchange(get(path), json_response({"total_count": 2, "jobs": jobs}))],
    )
    assert isinstance(result, ModelRunJobsResult)
    assert [(j.run_attempt, j.conclusion) for j in result.jobs] == [
        (1, "failure"),
        (2, "success"),
    ]
    assert result.jobs[0].steps[1].name == "Run pytest"
    assert result.jobs[0].steps[1].conclusion == "failure"
    assert result.truncated is False


# --- job_log_tail -----------------------------------------------------------------


def test_job_log_tail_keeps_only_the_end_of_the_log() -> None:
    log = b"".join(f"line {i}\n".encode() for i in range(20_000))
    result, _ = _run(
        _req(EnumGithubGatewayOperation.JOB_LOG_TAIL, job_id=77, tail_bytes=1024),
        [
            BytesExchange(
                get(f"{_BASE}/actions/jobs/77/logs"),
                status=200,
                body=log,
                limit=1024,
                keep="tail",
            )
        ],
    )
    assert isinstance(result, ModelJobLogTailResult)
    assert result.total_bytes == len(log)
    assert result.truncated is True
    assert result.text.encode() == log[-1024:]
    assert result.text.endswith("line 19999\n")


def test_job_log_tail_short_log_is_whole() -> None:
    result, _ = _run(
        _req(EnumGithubGatewayOperation.JOB_LOG_TAIL, job_id=78),
        [
            BytesExchange(
                get(f"{_BASE}/actions/jobs/78/logs"),
                status=200,
                body=b"ok\n",
                limit=16 * 1024,
                keep="tail",
            )
        ],
    )
    assert isinstance(result, ModelJobLogTailResult)
    assert (result.text, result.truncated, result.total_bytes) == ("ok\n", False, 3)


def test_job_log_tail_refuses_a_cap_past_64_kib() -> None:
    with pytest.raises(ValidationError):
        _req(EnumGithubGatewayOperation.JOB_LOG_TAIL, job_id=1, tail_bytes=65 * 1024)


def test_job_log_tail_gone_log_raises() -> None:
    with pytest.raises(GitHubTransportError, match="HTTP 410"):
        _run(
            _req(EnumGithubGatewayOperation.JOB_LOG_TAIL, job_id=79),
            [
                BytesExchange(
                    get(f"{_BASE}/actions/jobs/79/logs"),
                    status=410,
                    body=b'{"message":"Gone"}',
                    limit=16 * 1024,
                    keep="tail",
                )
            ],
        )


# --- artifacts -------------------------------------------------------------------

_LIST = f"{_BASE}/actions/runs/{_RUN}/artifacts?per_page=30"


def _listing(*items: dict[str, object]) -> JsonExchange:
    return JsonExchange(
        get(_LIST), json_response({"total_count": len(items), "artifacts": list(items)})
    )


def test_artifacts_lists_without_fetching() -> None:
    result, transport = _run(
        _req(EnumGithubGatewayOperation.ARTIFACTS, run_id=_RUN),
        [_listing(artifact(11, run_id=_RUN, size=900))],
    )
    assert isinstance(result, ModelArtifactsResult)
    assert [a.name for a in result.artifacts] == ["coverage-11"]
    assert result.fetched is None
    assert result.fetch_refused is None
    assert len(transport.sent) == 1


def test_artifacts_fetches_an_archive_under_the_cap() -> None:
    archive = b"PK\x03\x04" + b"\x00" * 500
    result, _ = _run(
        _req(EnumGithubGatewayOperation.ARTIFACTS, run_id=_RUN, artifact_id=11),
        [
            _listing(artifact(11, run_id=_RUN, size=len(archive))),
            JsonExchange(
                get(f"{_BASE}/actions/artifacts/11"),
                json_response(artifact(11, run_id=_RUN, size=len(archive))),
            ),
            BytesExchange(
                get(f"{_BASE}/actions/artifacts/11/zip"),
                status=200,
                body=archive,
                limit=256 * 1024,
                keep="head",
            ),
        ],
    )
    assert isinstance(result, ModelArtifactsResult)
    assert result.fetch_refused is None
    assert result.fetched is not None
    assert base64.b64decode(result.fetched.content_base64) == archive
    assert result.fetched.sha256 == hashlib.sha256(archive).hexdigest()
    assert result.fetched.size_bytes == len(archive)


def test_artifacts_refuses_an_archive_over_the_cap_without_downloading() -> None:
    big = artifact(12, run_id=_RUN, size=300 * 1024)
    result, transport = _run(
        _req(EnumGithubGatewayOperation.ARTIFACTS, run_id=_RUN, artifact_id=12),
        [
            _listing(big),
            JsonExchange(get(f"{_BASE}/actions/artifacts/12"), json_response(big)),
        ],
    )
    assert isinstance(result, ModelArtifactsResult)
    assert result.fetched is None
    assert result.fetch_refused is EnumArtifactFetchRefusal.OVER_SIZE_CAP
    assert not any(r.path.endswith("/zip") for r in transport.sent)


def test_artifacts_refuses_when_the_download_runs_past_the_cap() -> None:
    small_claim = artifact(13, run_id=_RUN, size=10)
    result, _ = _run(
        _req(
            EnumGithubGatewayOperation.ARTIFACTS,
            run_id=_RUN,
            artifact_id=13,
            max_bytes=100,
        ),
        [
            _listing(small_claim),
            JsonExchange(
                get(f"{_BASE}/actions/artifacts/13"), json_response(small_claim)
            ),
            BytesExchange(
                get(f"{_BASE}/actions/artifacts/13/zip"),
                status=200,
                body=b"x" * 200_000,
                limit=100,
                keep="head",
            ),
        ],
    )
    assert isinstance(result, ModelArtifactsResult)
    assert result.fetched is None
    assert result.fetch_refused is EnumArtifactFetchRefusal.OVER_SIZE_CAP


@pytest.mark.parametrize(
    ("meta", "status", "reason"),
    [
        (artifact(14, run_id=_RUN, size=10, expired=True), 200, "expired"),
        (artifact(14, run_id=999, size=10), 200, "not_in_run"),
        ({"message": "Not Found"}, 404, "not_in_run"),
    ],
)
def test_artifacts_refusals_are_typed(
    meta: dict[str, object], status: int, reason: str
) -> None:
    result, transport = _run(
        _req(EnumGithubGatewayOperation.ARTIFACTS, run_id=_RUN, artifact_id=14),
        [
            _listing(),
            JsonExchange(
                get(f"{_BASE}/actions/artifacts/14"),
                json_response(meta, status=status),
            ),
        ],
    )
    assert isinstance(result, ModelArtifactsResult)
    assert result.fetch_refused == EnumArtifactFetchRefusal(reason)
    assert not any(r.path.endswith("/zip") for r in transport.sent)


def test_artifact_id_is_refused_on_any_other_operation() -> None:
    with pytest.raises(ValidationError, match="artifact_id"):
        _req(EnumGithubGatewayOperation.RUN_JOBS, run_id=_RUN, artifact_id=1)


# --- pr_text ----------------------------------------------------------------------


def test_pr_text_reads_title_body_and_paged_comments() -> None:
    first = f"{_BASE}/issues/2962/comments?per_page=2"
    second = f"{first}&page=2"
    long_body = "b" * (MAX_TEXT_CHARS + 10)
    result, _ = _run(
        _req(EnumGithubGatewayOperation.PR_TEXT, pr_number=2962, limit=2),
        [
            JsonExchange(
                get(f"{_BASE}/pulls/2962"),
                json_response(pull_request(2962, body=long_body)),
            ),
            JsonExchange(
                get(first),
                json_response(
                    [issue_comment(1, body="first"), issue_comment(2, body="second")],
                    next_path=second,
                ),
            ),
        ],
    )
    assert isinstance(result, ModelPrTextResult)
    assert result.title == "feat: an example"
    assert result.body_truncated is True
    assert len(result.body) == MAX_TEXT_CHARS
    assert (result.head_ref, result.head_sha, result.base_ref) == (
        "jonah/omn-20912-example",
        HEAD,
        "dev",
    )
    assert result.author == "pr-author"
    assert [c.body for c in result.comments] == ["first", "second"]
    assert result.comments[0].author == "reviewer-bot"
    assert result.total_count == 3
    assert result.truncated is True


# --- releases ---------------------------------------------------------------------


def test_releases_reads_releases_and_tags() -> None:
    result, _ = _run(
        _req(EnumGithubGatewayOperation.RELEASES, limit=5),
        [
            JsonExchange(
                get(f"{_BASE}/releases?per_page=5"),
                json_response([release(1, "v0.4.310"), release(2, "v0.4.309")]),
            ),
            JsonExchange(
                get(f"{_BASE}/tags?per_page=5"),
                json_response([tag("v0.4.310", HEAD)]),
            ),
        ],
    )
    assert isinstance(result, ModelReleasesResult)
    assert [r.tag_name for r in result.releases] == ["v0.4.310", "v0.4.309"]
    assert result.tags[0].sha == HEAD
    assert (result.releases_truncated, result.tags_truncated) == (False, False)


# --- protocol A for the new operations, and the wire round trip ------------------


@pytest.mark.parametrize("op", list(SCM_READ_RESULT_MODELS))
def test_scm_read_result_discriminator_is_its_operation(
    op: EnumGithubGatewayOperation,
) -> None:
    model = SCM_READ_RESULT_MODELS[op]
    assert model.model_fields["operation"].default == op.value


def test_response_wrapper_carries_an_scm_read_result() -> None:
    result, _ = _run(
        _req(EnumGithubGatewayOperation.RELEASES, limit=1),
        [
            JsonExchange(
                get(f"{_BASE}/releases?per_page=1"), json_response([release(1, "v1")])
            ),
            JsonExchange(get(f"{_BASE}/tags?per_page=1"), json_response([])),
        ],
    )
    assert isinstance(result, ModelReleasesResult)
    wrapped = ModelGithubGatewayResponse.model_validate(
        {"correlation_id": "00000000-0000-4000-8000-000000020912", "result": result}
    )
    reparsed = ModelGithubGatewayResponse.model_validate(
        wrapped.model_dump(mode="json")
    )
    assert isinstance(reparsed.result, ModelReleasesResult)


@pytest.mark.parametrize(
    ("op", "missing"),
    [
        (EnumGithubGatewayOperation.RUN_JOBS, "run_id"),
        (EnumGithubGatewayOperation.ARTIFACTS, "run_id"),
        (EnumGithubGatewayOperation.JOB_LOG_TAIL, "job_id"),
        (EnumGithubGatewayOperation.PR_TEXT, "pr_number"),
    ],
)
def test_scoped_operations_require_their_id(
    op: EnumGithubGatewayOperation, missing: str
) -> None:
    with pytest.raises(ValidationError, match=missing):
        _req(op)


def test_list_limit_is_bounded() -> None:
    with pytest.raises(ValidationError):
        _req(EnumGithubGatewayOperation.WORKFLOW_RUNS, limit=301)


# --- the transport pieces these reads rely on --------------------------------------


def test_next_page_refuses_a_link_off_the_api_root() -> None:
    response = json_response({})
    hostile = response.model_copy(
        update={"headers": {"Link": '<https://evil.example/x?page=2>; rel="next"'}}
    )
    with pytest.raises(GithubLandingRequestError, match="leaves the API root"):
        next_page_request(hostile)


def test_next_page_is_none_without_a_next_link() -> None:
    response = json_response({})
    last = response.model_copy(
        update={"headers": {"Link": '<https://api.github.com/x?page=1>; rel="prev"'}}
    )
    assert next_page_request(last) is None


def test_read_capped_head_and_tail() -> None:
    data = bytes(range(256)) * 10
    assert _read_capped(io.BytesIO(data), limit=10, keep="head") == (
        data[:10],
        len(data),
        True,
    )
    assert _read_capped(io.BytesIO(data), limit=10, keep="tail") == (
        data[-10:],
        len(data),
        True,
    )
    assert _read_capped(io.BytesIO(b"abc"), limit=10, keep="tail") == (b"abc", 3, False)


def test_redirect_drops_authorization_when_the_host_changes() -> None:
    handler = _AuthStrippingRedirectHandler()
    original = urllib.request.Request(
        f"https://api.github.com{_BASE}/actions/jobs/1/logs",
        headers={"Authorization": "Bearer redacted-test-value"},
    )
    off_host = handler.redirect_request(
        original,
        io.BytesIO(),
        302,
        "Found",
        http.client.HTTPMessage(),
        "https://results-receiver.actions.githubusercontent.com/logs/1",
    )
    same_host = handler.redirect_request(
        original,
        io.BytesIO(),
        302,
        "Found",
        http.client.HTTPMessage(),
        f"https://api.github.com{_BASE}/actions/jobs/1/logs?moved=1",
    )
    assert off_host is not None
    assert not off_host.has_header("Authorization")
    assert same_host is not None
    assert same_host.has_header("Authorization")
