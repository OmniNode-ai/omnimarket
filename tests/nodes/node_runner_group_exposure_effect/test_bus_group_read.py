# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_runner_group_exposure_effect: the runner group is read through the GitHub write node (OMN-24326).

The source's own App token may lack the organization runner permission (the read ends
org-read-denied), while the write node's App holds it. With ``group_read`` declared, the group's
settings and repositories come from the write node's ``runner_group_get`` over the bus and the
source makes no organization call with its own token.

Seam: the request the reader builds is validated by the write node's own request model, and the
write node's real handler (on a recorded transport) produces the terminal payload the reader
parses, so a change of shape on either side fails here. Golden chain: the group is read through
the node while the local GitHub fixture refuses every organization call. Error chains: the node
unreachable, refusing, answering an older shape. Failing control: with ``group_read`` null the
same refused organization call reads org-read-denied.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, override
from uuid import UUID

import pytest
from pydantic import JsonValue, ValidationError

from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.models.model_repo_write_io import (
    EnumRepoWriteMode,
    EnumRepoWriteOperation,
    ModelRepoWriteFailed,
    ModelRepoWriteRequest,
)
from omnimarket.models.runner_group_exposure import EnumExposureReadFailure
from omnimarket.nodes.node_github_repo_write_effect.handlers.handler_github_repo_write import (
    HandlerGithubRepoWriteEffect,
)
from omnimarket.nodes.node_github_repo_write_effect.models import (
    ModelRepoWriteConfig,
)
from omnimarket.nodes.node_github_repo_write_effect.overlay import (
    ModelPrLabelOverlay,
    ModelRepoWriteOverlay,
    ModelRunnerGroupEntry,
    ModelRunnerGroupReadOverlay,
)
from omnimarket.nodes.node_github_repo_write_effect.services.runner_registration import (
    get_runner_group_request,
    list_runner_group_repositories_request,
)
from omnimarket.nodes.node_runner_group_exposure_effect.adapters.write_node_group_reader import (
    GroupReadError,
    WriteCall,
    WriteNodeGroupReader,
)
from omnimarket.nodes.node_runner_group_exposure_effect.handlers.handler_exposure_probe import (
    HandlerExposureProbe,
    load_topics,
)
from omnimarket.nodes.node_runner_group_exposure_effect.models.model_exposure_probe import (
    ModelExposureProbeRequest,
)
from omnimarket.nodes.node_runner_group_exposure_effect.overlay import (
    NODE_NAME,
    ModelGithubSource,
    ModelWriteNodeGroupRead,
)
from tests.node_overlay_support import install_node_overlay
from tests.nodes.node_runner_group_exposure_effect.test_exposure_probe import (
    IDENTITY,
    NOW,
    REPO,
    SETTINGS,
    TOKEN_ENV,
    TOKEN_VALUE,
    Scenario,
    _Handler,
    _Server,
)
from tests.nodes.node_runner_group_exposure_effect.test_token_mint import (
    MINT,
    MINTED,
    _Minter,
)

pytestmark = pytest.mark.unit

ORG = "example-org"
GROUP = 7
ROUTE = ModelWriteNodeGroupRead(
    bus_lane="example-lane",
    workspace_env="EXAMPLE_WORKSPACE_ROOT",
    anchor_repository=REPO,
    requested_by_lane="example-exposure-reader",
    timeout_seconds=5,
)
_RATE = {
    "x-ratelimit-limit": "5000",
    "x-ratelimit-remaining": "4900",
    "x-ratelimit-used": "100",
    "x-ratelimit-reset": "1791003600",
    "x-ratelimit-resource": "core",
}


class _PathServer(_Server):
    """The shared fixture server, also recording every path it is asked."""

    seen_paths: list[str]


class _RecordingHandler(_Handler):
    server: _PathServer

    @override
    def do_GET(self) -> None:
        self.server.seen_paths.append(self.path)
        super().do_GET()


@pytest.fixture
def server() -> Iterator[_PathServer]:
    import threading

    httpd = _PathServer(("127.0.0.1", 0), _RecordingHandler)
    httpd.scenario = Scenario()
    httpd.seen_auth = []
    httpd.seen_paths = []
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def _paths(server: _PathServer) -> list[str]:
    return list(server.seen_paths)


@dataclass
class _Transport:
    """The write node's GitHub transport: a strict, ordered list of exchanges."""

    script: list[tuple[ModelGithubHttpRequest, ModelGithubHttpResponse]]
    sent: list[ModelGithubHttpRequest] = field(default_factory=list)

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        assert self.script, f"unexpected request {request.method} {request.path}"
        expected, response = self.script.pop(0)
        assert request == expected
        self.sent.append(request)
        return response


class _NoClaims:
    def active_claim(self, *, claims_dir: str, pr_key: str, now: str) -> None:
        return None


def _ok(body: dict[str, Any]) -> ModelGithubHttpResponse:
    return ModelGithubHttpResponse(status=200, headers=_RATE, body=body)


def _group_body(**over: object) -> dict[str, Any]:
    return {
        "id": GROUP,
        "name": "example-group",
        "allows_public_repositories": False,
        "restricted_to_selected_workflows": True,
        "selected_workflows": [IDENTITY],
        **over,
    }


def _overlay(group: int = GROUP) -> ModelRepoWriteOverlay:
    return ModelRepoWriteOverlay(
        pr_label=ModelPrLabelOverlay(allowed_labels=("lab:ready",)),
        runner_group_reads=ModelRunnerGroupReadOverlay(
            entries=(ModelRunnerGroupEntry(organization=ORG, group_id=group),)
        ),
    )


@dataclass
class _Node:
    """The write node's REAL handler behind a WriteCall, on a recorded GitHub transport."""

    group_body: dict[str, Any] = field(default_factory=_group_body)
    repositories: list[str] = field(default_factory=lambda: [REPO])
    overlay_group: int = GROUP
    requests: list[ModelRepoWriteRequest] = field(default_factory=list)

    def call(self) -> WriteCall:
        def call(request: ModelRepoWriteRequest) -> tuple[bool, dict[str, JsonValue]]:
            self.requests.append(request)
            transport = _Transport(
                [
                    (get_runner_group_request(ORG, GROUP), _ok(self.group_body)),
                    (
                        list_runner_group_repositories_request(ORG, GROUP, 1),
                        _ok(
                            {
                                "repositories": [
                                    {"full_name": r} for r in self.repositories
                                ]
                            }
                        ),
                    ),
                ]
            )
            handler = HandlerGithubRepoWriteEffect(
                transport,
                claim_lookup=_NoClaims(),
                runtime_claims_dir="",
                config=ModelRepoWriteConfig(
                    github_mode=EnumRepoWriteMode.DRY_RUN,
                    github_mode_by_repository={REPO: EnumRepoWriteMode.ENFORCE},
                ),
                clock=lambda: 1_791_000_000.0,
                overlay=_overlay(self.overlay_group),
            )
            result = asyncio.run(handler.handle(request))
            payload: dict[str, JsonValue] = result.model_dump(mode="json")
            return isinstance(result, ModelRepoWriteFailed), payload

        return call


def _probe(
    server: _PathServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    call: WriteCall | None,
    *,
    group_read: ModelWriteNodeGroupRead | None = ROUTE,
    scenario: Scenario | None = None,
    minter: _Minter | None = None,
) -> tuple[HandlerExposureProbe, _Minter]:
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    server.scenario = scenario if scenario is not None else Scenario()
    source = {
        "backend": "github",
        "api_base": f"http://127.0.0.1:{server.server_address[1]}",
        "org": ORG,
        "group_id": GROUP,
        "group_labels": ["example-private-group"],
        "group_read": group_read.model_dump(mode="json") if group_read else None,
        "token_env": TOKEN_ENV,
        "token_mint": MINT,
        "org_read_permission": "example_org_permission_read",
        "max_calls": 60,
        "timeout_seconds": 5,
    }
    install_node_overlay(
        monkeypatch, tmp_path, NODE_NAME, {"settings": SETTINGS, "source": source}
    )
    minted = minter if minter is not None else _Minter()
    reader = (
        WriteNodeGroupReader(group_read, call)
        if group_read is not None and call is not None
        else None
    )
    return HandlerExposureProbe(token_mint=minted, group_reader=reader), minted


def _request() -> ModelExposureProbeRequest:
    return ModelExposureProbeRequest(correlation_id="bus-group-1", now=NOW)


# -- golden chain ------------------------------------------------------------------------------


def test_golden_chain_the_group_is_read_through_the_write_node_and_not_with_the_source_token(
    server: _PathServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = _Node()
    # the fixture refuses every organization call: only the write node can have read the group
    probe, minter = _probe(
        server,
        tmp_path,
        monkeypatch,
        node.call(),
        scenario=Scenario(group_status=403),
    )
    result = probe.handle(_request())
    assert result.reading.verdict is EnumReadingVerdict.WORKING
    assert result.reading.read_failure is None
    assert result.missing_permission is None
    topic, _ = HandlerExposureProbe.event_for(result)
    assert topic == load_topics()["probe_completed"]
    # the seam, request side: the write node's own model accepted what the reader built
    (sent,) = node.requests
    assert sent.operation is EnumRepoWriteOperation.RUNNER_GROUP_GET
    assert (sent.runner_group_id, sent.repo, sent.dry_run) == (GROUP, REPO, False)
    # the source's own token made no organization call and the group came from the node
    assert not [p for p in _paths(server) if p.startswith("/orgs/")]
    assert _paths(server)  # the repository reads still ran with the source's token
    assert set(server.seen_auth) == {f"Bearer {MINTED}"}
    assert minter.calls  # minted for the repository reads only


def test_the_write_node_facts_decide_the_verdict(
    server: _PathServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = _Node(group_body=_group_body(allows_public_repositories=True))
    probe, _ = _probe(server, tmp_path, monkeypatch, node.call())
    result = probe.handle(_request())
    assert result.reading.verdict is EnumReadingVerdict.NOT_WORKING


# -- named failing control ---------------------------------------------------------------------


def test_failing_control_without_group_read_the_same_refusal_reads_org_read_denied(
    server: _PathServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe, _ = _probe(
        server,
        tmp_path,
        monkeypatch,
        None,
        group_read=None,
        scenario=Scenario(group_status=403),
    )
    result = probe.handle(_request())
    assert result.reading.verdict is EnumReadingVerdict.UNKNOWN
    assert result.reading.read_failure is EnumExposureReadFailure.ORG_READ_DENIED
    assert "/orgs/example-org/actions/runner-groups/7" in _paths(server)


# -- error chains ------------------------------------------------------------------------------


def _raises(failure: EnumExposureReadFailure) -> WriteCall:
    def call(request: ModelRepoWriteRequest) -> tuple[bool, dict[str, JsonValue]]:
        raise GroupReadError(failure)

    return call


def _answers(failed: bool, payload: dict[str, JsonValue]) -> WriteCall:
    def call(request: ModelRepoWriteRequest) -> tuple[bool, dict[str, JsonValue]]:
        return failed, payload

    return call


def _unknown_with(
    server: _PathServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    call: WriteCall,
) -> tuple[Any, _Minter]:
    probe, minter = _probe(server, tmp_path, monkeypatch, call)
    result = probe.handle(_request())
    assert result.reading.verdict is EnumReadingVerdict.UNKNOWN
    topic, _ = HandlerExposureProbe.event_for(result)
    assert topic == load_topics()["probe_failed"]
    assert TOKEN_VALUE not in result.model_dump_json()
    # a group that was not read makes no GitHub call and mints nothing
    assert server.seen_auth == []
    assert minter.calls == []
    return result, minter


def test_error_chain_a_write_node_that_does_not_answer_reads_unknown_with_its_own_reason(
    server: _PathServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _ = _unknown_with(
        server,
        tmp_path,
        monkeypatch,
        _raises(EnumExposureReadFailure.WRITE_NODE_UNAVAILABLE),
    )
    assert result.reading.read_failure is EnumExposureReadFailure.WRITE_NODE_UNAVAILABLE
    assert result.calls == 1
    assert result.missing_permission is None


def test_error_chain_a_group_the_overlay_does_not_name_is_refused_by_the_node(
    server: _PathServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = _Node(overlay_group=GROUP + 1)
    result, _ = _unknown_with(server, tmp_path, monkeypatch, node.call())
    assert result.reading.read_failure is EnumExposureReadFailure.GROUP_READ_REFUSED
    assert result.missing_permission is None


def test_error_chain_a_github_denial_at_the_write_node_names_the_declared_permission(
    server: _PathServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    denied = ModelRepoWriteFailed(
        correlation_id=UUID(int=1),
        operation=EnumRepoWriteOperation.RUNNER_GROUP_GET,
        repo=REPO,
        idempotency_key="k",
        requested_by_lane="example-exposure-reader",
        mode=EnumRepoWriteMode.ENFORCE,
        reason="github_refused",
        detail="refused",
        http_status=403,
    )
    result, _ = _unknown_with(
        server,
        tmp_path,
        monkeypatch,
        _answers(True, denied.model_dump(mode="json")),
    )
    assert result.reading.read_failure is EnumExposureReadFailure.ORG_READ_DENIED
    assert result.missing_permission == "example_org_permission_read"


def test_error_chain_a_rejected_login_at_the_write_node_names_no_permission(
    server: _PathServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    denied = ModelRepoWriteFailed(
        correlation_id=UUID(int=1),
        operation=EnumRepoWriteOperation.RUNNER_GROUP_GET,
        repo=REPO,
        idempotency_key="k",
        requested_by_lane="example-exposure-reader",
        mode=EnumRepoWriteMode.ENFORCE,
        reason="github_refused",
        detail="refused",
        http_status=401,
    )
    result, _ = _unknown_with(
        server,
        tmp_path,
        monkeypatch,
        _answers(True, denied.model_dump(mode="json")),
    )
    assert result.reading.read_failure is EnumExposureReadFailure.ORG_READ_DENIED
    assert result.missing_permission is None


def test_error_chain_a_missing_permission_denial_404_at_the_write_node_names_the_declared_permission(
    server: _PathServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    denied = ModelRepoWriteFailed(
        correlation_id=UUID(int=1),
        operation=EnumRepoWriteOperation.RUNNER_GROUP_GET,
        repo=REPO,
        idempotency_key="k",
        requested_by_lane="example-exposure-reader",
        mode=EnumRepoWriteMode.ENFORCE,
        reason="github_refused",
        detail="refused",
        http_status=404,
    )
    result, _ = _unknown_with(
        server,
        tmp_path,
        monkeypatch,
        _answers(True, denied.model_dump(mode="json")),
    )
    assert result.reading.read_failure is EnumExposureReadFailure.ORG_READ_DENIED
    assert result.missing_permission == "example_org_permission_read"


def test_error_chain_an_older_write_node_shape_reads_group_read_refused_or_unreadable(
    server: _PathServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused, _ = _unknown_with(
        server,
        tmp_path,
        monkeypatch,
        _answers(True, {"error": "ENVELOPE_VALIDATION_FAILED"}),
    )
    assert refused.reading.read_failure is EnumExposureReadFailure.GROUP_READ_REFUSED


def test_error_chain_a_completed_answer_without_the_group_reads_unreadable(
    server: _PathServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _ = _unknown_with(
        server, tmp_path, monkeypatch, _answers(False, {"operation": "runner_list"})
    )
    assert result.reading.read_failure is EnumExposureReadFailure.UNREADABLE


def test_error_chain_the_answer_for_another_group_is_not_used(
    server: _PathServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inner = _Node().call()

    def call(request: ModelRepoWriteRequest) -> tuple[bool, dict[str, JsonValue]]:
        failed, payload = inner(request)
        group = payload["runner_group"]
        assert isinstance(group, dict)
        group["id"] = GROUP + 1
        return failed, payload

    result, _ = _unknown_with(server, tmp_path, monkeypatch, call)
    assert result.reading.read_failure is EnumExposureReadFailure.UNREADABLE


# -- the settings ------------------------------------------------------------------------------


def test_the_anchor_repository_must_belong_to_the_sources_org() -> None:
    other = ROUTE.model_copy(
        update={"anchor_repository": "example-other-org/example-repo"}
    )
    with pytest.raises(ValidationError):
        ModelGithubSource(
            backend="github",
            api_base="http://127.0.0.1:1",
            org=ORG,
            group_id=GROUP,
            group_labels=("example-private-group",),
            group_read=other,
            token_env=TOKEN_ENV,
            token_mint=None,
            org_read_permission="example_org_permission_read",
            max_calls=1,
            timeout_seconds=1,
        )


class _NeverCalled:
    def get(
        self, url: str, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]:
        raise AssertionError("no HTTP call is made at construction")


def test_a_declared_group_read_with_no_reader_is_refused_at_construction() -> None:
    from omnimarket.nodes.node_runner_group_exposure_effect.adapters.github_group_source import (
        GithubGroupSource,
    )

    source = ModelGithubSource(
        backend="github",
        api_base="http://127.0.0.1:1",
        org=ORG,
        group_id=GROUP,
        group_labels=("example-private-group",),
        group_read=ROUTE,
        token_env=TOKEN_ENV,
        token_mint=None,
        org_read_permission="example_org_permission_read",
        max_calls=1,
        timeout_seconds=1,
    )
    with pytest.raises(ValueError, match="group_read"):
        GithubGroupSource(source, _NeverCalled(), {})


def test_an_overlay_written_before_group_read_existed_still_loads_as_a_direct_read() -> (
    None
):
    """The main overlays branch holds an overlay with no group_read key; it must not be refused."""
    source = ModelGithubSource.model_validate(
        {
            "backend": "github",
            "api_base": "http://127.0.0.1:1",
            "org": ORG,
            "group_id": GROUP,
            "group_labels": ["example-private-group"],
            "token_env": TOKEN_ENV,
            "token_mint": None,
            "org_read_permission": "example_org_permission_read",
            "max_calls": 1,
            "timeout_seconds": 1,
        }
    )
    assert source.group_read is None
