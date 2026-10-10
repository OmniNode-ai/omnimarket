# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Conformance of HandlerSourceControlGithub to omnibase_spi (OMN-20912).

Structural conformance: the adapter is an instance of the runtime-checkable
``ProtocolSourceControl`` and ``ProtocolCodeRepository``, and every protocol
method's parameters match. Behaviour on a strict recorded fake transport:
each served operation sends the recorded requests and returns the SPI wire
models; create_pr goes through the write effect's pr_create; every operation
not served raises ``SourceControlOperationNotSupportedError`` naming itself.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field

import pytest
from omnibase_spi.contracts.services.contract_source_control_types import (
    ModelCIStatus,
    ModelDiff,
    ModelPullRequest,
)
from omnibase_spi.protocols.overseer.protocol_code_repository import (
    ProtocolCodeRepository,
)
from omnibase_spi.protocols.services.protocol_source_control import (
    ProtocolSourceControl,
)
from omnibase_spi.protocols.types.protocol_service_types import (
    ProtocolServiceHealthStatus,
)

from omnimarket.github_landing.github_landing_requests import (
    compare_request,
    create_pull_request_request,
    head_check_runs_request,
    open_pulls_for_head_request,
    pull_request_request,
    pull_requests_request,
)
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.nodes.node_github_repo_write_effect.handlers.handler_source_control_github import (
    HandlerSourceControlGithub,
)
from omnimarket.nodes.node_github_repo_write_effect.models import (
    SourceControlOperationNotSupportedError,
)

pytestmark = pytest.mark.unit

_REPO = "OmniNode-ai/omnimarket"
_HEAD = "4ceedeafeb4040c987883d96c20e745ad5eed6e1"
_NOW = 1_791_000_000.0
_HEADERS = {
    "x-ratelimit-limit": "5000",
    "x-ratelimit-remaining": "4900",
    "x-ratelimit-used": "100",
    "x-ratelimit-reset": str(int(_NOW) + 3600),
    "x-ratelimit-resource": "core",
}
_API = "https://api.github.com"


def _ok(
    body: dict[str, object] | list[object] | None,
    *,
    status: int = 200,
    next_path: str | None = None,
) -> ModelGithubHttpResponse:
    headers = dict(_HEADERS)
    if next_path is not None:
        headers["Link"] = f'<{_API}{next_path}>; rel="next"'
    wrapped = {"value": body} if isinstance(body, list) else body
    return ModelGithubHttpResponse(status=status, headers=headers, body=wrapped)


def _pr(number: int, *, merged: bool = False) -> dict[str, object]:
    return {
        "number": number,
        "title": f"pr {number}",
        "state": "closed" if merged else "open",
        "merged_at": "2026-10-10T18:30:00Z" if merged else None,
        "mergeable": True,
        "user": {"login": "pr-author"},
        "head": {"ref": f"branch-{number}", "sha": _HEAD},
        "base": {"ref": "dev"},
        "html_url": f"https://github.com/{_REPO}/pull/{number}",
        "created_at": "2026-10-10T18:00:00Z",
        "updated_at": "2026-10-10T18:20:00Z",
    }


@dataclass
class _Recorded:
    exchanges: list[tuple[ModelGithubHttpRequest, ModelGithubHttpResponse]]
    sent: list[ModelGithubHttpRequest] = field(default_factory=list)

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        self.sent.append(request)
        assert self.exchanges, f"no recorded exchange left for {request!r}"
        expected, response = self.exchanges.pop(0)
        assert request == expected, f"sent {request!r}, recorded {expected!r}"
        return response


async def _adapter(
    exchanges: list[tuple[ModelGithubHttpRequest, ModelGithubHttpResponse]],
) -> tuple[HandlerSourceControlGithub, _Recorded]:
    fake = _Recorded(exchanges)
    adapter = HandlerSourceControlGithub(fake, clock=lambda: _NOW)
    assert await adapter.connect() is True
    return adapter, fake


# --- structural conformance --------------------------------------------------------


@pytest.mark.parametrize("protocol", [ProtocolSourceControl, ProtocolCodeRepository])
def test_adapter_is_an_instance_of_the_spi_protocol(protocol: type) -> None:
    assert isinstance(HandlerSourceControlGithub(), protocol)


@pytest.mark.parametrize("protocol", [ProtocolSourceControl, ProtocolCodeRepository])
def test_every_protocol_method_has_the_protocols_parameters(protocol: type) -> None:
    for name, member in inspect.getmembers(protocol, inspect.isfunction):
        if name.startswith("_"):
            continue
        ours = getattr(HandlerSourceControlGithub, name)
        assert inspect.iscoroutinefunction(ours), f"{name} must be async"
        theirs_params = list(inspect.signature(member).parameters)
        ours_params = list(inspect.signature(ours).parameters)
        assert ours_params == theirs_params, f"{name}: {ours_params} != {theirs_params}"


def test_the_adapter_satisfies_both_protocols_statically() -> None:
    # mypy proves these assignments; at runtime they only have to construct.
    source_control: ProtocolSourceControl = HandlerSourceControlGithub()
    code_repository: ProtocolCodeRepository = HandlerSourceControlGithub()
    assert isinstance(source_control, HandlerSourceControlGithub)
    assert isinstance(code_repository, HandlerSourceControlGithub)


async def test_health_check_and_capabilities() -> None:
    adapter, _ = await _adapter([])
    health = await adapter.health_check()
    assert isinstance(health, ProtocolServiceHealthStatus)
    assert health.is_healthy()
    assert await adapter.get_capabilities() == ["read", "write"]
    await adapter.close()
    assert not (await adapter.health_check()).is_healthy()


# --- served reads ----------------------------------------------------------------


async def test_list_prs_pages_until_the_limit() -> None:
    first = pull_requests_request(_REPO, state="all", per_page=3)
    second = f"{first.path}&page=2"
    adapter, fake = await _adapter(
        [
            (first, _ok([_pr(1), _pr(2, merged=True)], next_path=second)),
            (
                ModelGithubHttpRequest(method="GET", path=second),
                _ok([_pr(3), _pr(4)]),
            ),
        ]
    )
    prs = await adapter.list_prs(_REPO, state="all", limit=3)
    assert [p.number for p in prs] == [1, 2, 3]
    assert prs[1].state == "merged"
    assert all(isinstance(p, ModelPullRequest) for p in prs)
    assert len(fake.sent) == 2


async def test_get_pr_maps_the_wire_model_and_404_is_a_key_error() -> None:
    adapter, _ = await _adapter(
        [
            (pull_request_request(_REPO, 7), _ok(_pr(7))),
            (pull_request_request(_REPO, 8), _ok({"message": "Not Found"}, status=404)),
        ]
    )
    pr = await adapter.get_pr(_REPO, 7)
    assert (pr.head_ref, pr.base_ref, pr.author, pr.mergeable) == (
        "branch-7",
        "dev",
        "pr-author",
        True,
    )
    with pytest.raises(KeyError):
        await adapter.get_pr(_REPO, 8)


async def test_get_ci_status_rolls_up_check_runs() -> None:
    runs = [
        {"name": "lint", "status": "completed", "conclusion": "success"},
        {"name": "tests", "status": "completed", "conclusion": "failure"},
    ]
    adapter, _ = await _adapter(
        [
            (
                head_check_runs_request(_REPO, _HEAD),
                _ok({"total_count": 2, "check_runs": runs}),
            )
        ]
    )
    status = await adapter.get_ci_status(_REPO, _HEAD)
    assert isinstance(status, ModelCIStatus)
    assert status.state == "failure"
    assert [c.name for c in status.checks] == ["lint", "tests"]


async def test_get_diff_counts_every_file() -> None:
    files = [
        {"filename": "a.py", "additions": 3, "deletions": 1, "patch": "@@ -1 +1 @@"},
        {
            "filename": "b.py",
            "additions": 2,
            "deletions": 0,
            "patch": "@@ -0,0 +1,2 @@",
        },
    ]
    adapter, _ = await _adapter(
        [(compare_request(_REPO, "dev", "feature"), _ok({"files": files}))]
    )
    diff = await adapter.get_diff(_REPO, "dev", "feature")
    assert isinstance(diff, ModelDiff)
    assert (diff.files_changed, diff.additions, diff.deletions) == (2, 5, 1)
    assert "--- a.py" in diff.patch


# --- served write ----------------------------------------------------------------


async def test_create_pr_goes_through_the_write_effect() -> None:
    created = _pr(4000)
    adapter, fake = await _adapter(
        [
            (open_pulls_for_head_request(_REPO, "branch-4000"), _ok([])),
            (
                create_pull_request_request(
                    _REPO,
                    title="t",
                    head="branch-4000",
                    base="dev",
                    body="b",
                    draft=False,
                ),
                _ok(created, status=201),
            ),
            (pull_request_request(_REPO, 4000), _ok(created)),
        ]
    )
    pr = await adapter.create_pr(_REPO, "t", "b", "branch-4000", base="dev")
    assert pr.number == 4000
    assert [r.method for r in fake.sent] == ["GET", "POST", "GET"]


# --- not served ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("operation", "args"),
    [
        ("merge_pr", (_REPO, 1)),
        ("enable_auto_merge", (_REPO, 1)),
        ("admin_merge", (_REPO, 1)),
        ("create_branch", (_REPO, "b")),
        ("push_branch", (_REPO, "b")),
        ("force_push", (_REPO, "b", _HEAD)),
        ("delete_branch", (_REPO, "b")),
        ("rebase", (_REPO, "b")),
    ],
)
async def test_operations_not_served_raise_a_typed_error(
    operation: str, args: tuple[object, ...]
) -> None:
    adapter, fake = await _adapter([])
    with pytest.raises(SourceControlOperationNotSupportedError) as raised:
        await getattr(adapter, operation)(*args)
    assert raised.value.operation == operation
    assert raised.value.reason
    assert fake.sent == []
