# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_github_repo_write_effect over its real contract (OMN-20912).

Topic bytes in, typed result out, through the runtime's own path: discovery
reads ``contract.yaml``, ``build_routing_map`` resolves the request topic to the
handler entry and its ``event_model``, ``RuntimeDispatch`` decodes the envelope
from an ``InMemoryTransport`` and coerces the payload into
``ModelRepoWriteRequest``, the handler runs over a strict recorded fake
transport, and the result goes out on the topic the contract names for its
class. Three legs: a pr_comment that posts (completed), a dry_run that sends
nothing (completed), and a pr_close with no claim (failed). The pr_label legs
run against an injected overlay: an allowed add (completed), a label outside
the allowed set (failed label_not_allowed), and a moved head (failed
head_moved). The repo_variable legs run against an overlay that also declares
the repository variables, with the value files under a temporary root: a set
that reads then creates (completed), a get reporting the value in sync
(completed), a name outside the overlay (failed variable_not_allowed), a name
carrying a refuse marker (failed secret_refused), and a refused POST (failed
github_refused).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.runtime.runtime_dispatch import RuntimeDispatch
from omnibase_core.runtime.transport.runtime_in_memory_broker import InMemoryBroker
from omnibase_core.runtime.transport.runtime_in_memory_transport import (
    InMemoryTransport,
)
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.core_runtime.dlq_resolver import derive_canonical_dlq_topic
from omnibase_infra.runtime.core_runtime.routing_map_builder import build_routing_map

from omnimarket.github_landing.github_landing_requests import (
    LIST_PAGE_SIZE,
    create_issue_comment_request,
    issue_comments_request,
    pull_request_request,
)
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.nodes.node_github_repo_write_effect.handlers import (
    HandlerGithubRepoWriteEffect,
)
from omnimarket.nodes.node_github_repo_write_effect.handlers.handler_github_repo_write import (
    idempotency_marker,
)
from omnimarket.nodes.node_github_repo_write_effect.handlers.handler_pr_claim_lookup import (
    HandlerPrClaimLookup,
)
from omnimarket.nodes.node_github_repo_write_effect.models import (
    EnumRepoWriteMode,
    EnumRepoWriteOperation,
    EnumRepoWriteRefusal,
    ModelRepoWriteCompleted,
    ModelRepoWriteFailed,
    ModelRepoWriteRequest,
)
from omnimarket.nodes.node_github_repo_write_effect.overlay import (
    ModelPrLabelOverlay,
    ModelRepoVariablesEntry,
    ModelRepoVariablesOverlay,
    ModelRepoWriteOverlay,
    ModelRunnerEntry,
    ModelRunnerGroupEntry,
    ModelRunnerGroupReadOverlay,
    ModelRunnerRemovalOverlay,
)
from omnimarket.nodes.node_github_repo_write_effect.services.pr_label import (
    add_labels_request,
)
from omnimarket.nodes.node_github_repo_write_effect.services.repo_variable import (
    create_variable_request,
    get_variable_request,
)
from omnimarket.nodes.node_github_repo_write_effect.services.runner_registration import (
    get_runner_group_request,
    list_runner_group_repositories_request,
    list_runners_request,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_CONTRACT = _ROOT / "src/omnimarket/nodes/node_github_repo_write_effect/contract.yaml"
_REQUESTED = "onex.cmd.omnimarket.github-repo-write-requested.v1"
_COMPLETED = "onex.evt.omnimarket.github-repo-write-completed.v1"
_FAILED = "onex.evt.omnimarket.github-repo-write-failed.v1"
_GROUP = "onex.core-runtime.github-repo-write"
_CLOCK = datetime(2026, 10, 10, 19, 0, 0, tzinfo=UTC)
_REPO = "OmniNode-ai/omnimarket"
_HEAD = "4ceedeafeb4040c987883d96c20e745ad5eed6e1"
_MOVED = "a7f20d7a" + "0" * 32
_KEY = b"OmniNode-ai/omnimarket#4000"
# Test-local label names only; no real label name lives in src, the contract
# or the shipped overlay example.
_LABEL = "lab:ready"
_OFF_LIMITS = "lab:off-limits"
_PR_URL = f"https://github.com/{_REPO}/pull/4000"
_HEADERS = {
    "x-ratelimit-limit": "5000",
    "x-ratelimit-remaining": "4900",
    "x-ratelimit-used": "100",
    "x-ratelimit-reset": str(int(_CLOCK.timestamp()) + 3600),
    "x-ratelimit-resource": "core",
}


@dataclass
class _Recorded:
    exchanges: list[tuple[ModelGithubHttpRequest, ModelGithubHttpResponse]]
    sent: list[ModelGithubHttpRequest] = field(default_factory=list)

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        self.sent.append(request)
        expected, response = self.exchanges.pop(0)
        assert request == expected, f"sent {request!r}, recorded {expected!r}"
        return response


def _chain(
    handler: HandlerGithubRepoWriteEffect,
) -> tuple[RuntimeDispatch, InMemoryTransport, InMemoryBroker]:
    manifest = discover_contracts_from_paths([_CONTRACT])
    assert not manifest.errors, manifest.errors
    (contract,) = manifest.contracts
    assert contract.event_bus is not None
    topics = frozenset(contract.event_bus.subscribe_topics)
    assert topics == {_REQUESTED}
    routing_map = build_routing_map(
        [contract], topics, handler_resolver=lambda _ref: handler
    )
    assert set(routing_map) == topics
    broker = InMemoryBroker(num_partitions=1)
    transport = InMemoryTransport(broker=broker, group=_GROUP, topics=sorted(topics))
    dispatch = RuntimeDispatch(
        consumer=transport,
        producer=transport,
        routing_map=routing_map,
        dlq_topic_resolver=derive_canonical_dlq_topic,
        clock=lambda: _CLOCK,
    )
    return dispatch, transport, broker


def _on(broker: InMemoryBroker, topic: str) -> list[ModelEventEnvelope[object]]:
    return [
        ModelEventEnvelope[object].model_validate_json(record.value)
        for record in broker.records(topic, 0)
    ]


async def _run(
    handler: HandlerGithubRepoWriteEffect, command: ModelRepoWriteRequest
) -> tuple[InMemoryBroker, UUID]:
    dispatch, transport, broker = _chain(handler)
    correlation_id = uuid4()
    envelope = ModelEventEnvelope(
        envelope_id=uuid4(),
        payload=command.model_dump(mode="json"),
        correlation_id=correlation_id,
        event_type="golden-chain.inbound",
        payload_type="ModelRepoWriteRequest",
    )
    await transport.send(
        _REQUESTED,
        key=_KEY,
        value=envelope.model_dump_json().encode("utf-8"),
        headers={},
    )
    assert await dispatch.drain() == 1
    return broker, correlation_id


def _command(op: EnumRepoWriteOperation, **kw: object) -> ModelRepoWriteRequest:
    return ModelRepoWriteRequest.model_validate(
        {
            "correlation_id": uuid4(),
            "operation": op,
            "repo": _REPO,
            "idempotency_key": "golden-chain-omn20912",
            "requested_by_lane": "scm-ops-5d21",
            **kw,
        }
    )


async def test_a_posted_comment_is_published_as_completed() -> None:
    marker = idempotency_marker("golden-chain-omn20912")
    fake = _Recorded(
        [
            (
                issue_comments_request(_REPO, 4000, per_page=LIST_PAGE_SIZE),
                ModelGithubHttpResponse(
                    status=200, headers=_HEADERS, body={"value": []}
                ),
            ),
            (
                create_issue_comment_request(_REPO, 4000, f"lab note\n\n{marker}"),
                ModelGithubHttpResponse(
                    status=201, headers=_HEADERS, body={"id": 91, "html_url": "u"}
                ),
            ),
        ]
    )
    handler = HandlerGithubRepoWriteEffect(fake, clock=_CLOCK.timestamp)
    broker, correlation_id = await _run(
        handler,
        _command(EnumRepoWriteOperation.PR_COMMENT, pr_number=4000, body="lab note"),
    )
    (envelope,) = _on(broker, _COMPLETED)
    assert envelope.correlation_id == correlation_id
    completed = ModelRepoWriteCompleted.model_validate(envelope.payload)
    assert completed.mode is EnumRepoWriteMode.ENFORCE
    assert completed.comment_id == 91
    assert _on(broker, _FAILED) == []


async def test_a_dry_run_sends_nothing_and_completes() -> None:
    fake = _Recorded([])
    handler = HandlerGithubRepoWriteEffect(fake, clock=_CLOCK.timestamp)
    broker, _ = await _run(
        handler,
        _command(
            EnumRepoWriteOperation.WORKFLOW_DISPATCH,
            workflow="ci.yml",
            ref="dev",
            dry_run=True,
        ),
    )
    assert fake.sent == []
    (envelope,) = _on(broker, _COMPLETED)
    completed = ModelRepoWriteCompleted.model_validate(envelope.payload)
    assert completed.mode is EnumRepoWriteMode.DRY_RUN
    assert completed.requests


async def test_a_close_without_a_claim_is_published_as_failed(tmp_path: Path) -> None:
    fake = _Recorded([])
    handler = HandlerGithubRepoWriteEffect(
        fake,
        claim_lookup=HandlerPrClaimLookup(),
        runtime_claims_dir="",
        clock=_CLOCK.timestamp,
    )
    broker, _ = await _run(
        handler,
        _command(
            EnumRepoWriteOperation.PR_CLOSE,
            pr_number=4000,
            expected_head_sha=_HEAD,
            claims_dir=str(tmp_path),
            requested_by_run="run-golden",
        ),
    )
    assert fake.sent == []
    assert _on(broker, _COMPLETED) == []
    (envelope,) = _on(broker, _FAILED)
    failed = ModelRepoWriteFailed.model_validate(envelope.payload)
    assert failed.reason is EnumRepoWriteRefusal.CLAIM_NOT_HELD
    assert failed.terminal_failure_cause is EnumRepoWriteRefusal.CLAIM_NOT_HELD


async def test_a_close_carrying_the_callers_claim_crosses_the_bus_and_completes() -> (
    None
):
    # OMN-21067 AC1: the claim is not in any registry the runtime reads; it
    # travels on the command envelope and the runtime accepts it.
    fake = _Recorded([])
    handler = HandlerGithubRepoWriteEffect(
        fake,
        claim_lookup=HandlerPrClaimLookup(),
        runtime_claims_dir="",
        clock=_CLOCK.timestamp,
    )
    taken = (_CLOCK - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    broker, _ = await _run(
        handler,
        _command(
            EnumRepoWriteOperation.PR_CLOSE,
            pr_number=4000,
            expected_head_sha=_HEAD,
            requested_by_run="run-golden",
            dry_run=True,
            claim={
                "pr_key": "omninode-ai/omnimarket#4000",
                "lane_id": "scm-ops-5d21",
                "claimed_by_run": "run-golden",
                "action": "close",
                "claimed_at": taken,
            },
        ),
    )
    assert fake.sent == []
    assert _on(broker, _FAILED) == []
    (envelope,) = _on(broker, _COMPLETED)
    completed = ModelRepoWriteCompleted.model_validate(envelope.payload)
    assert completed.operation is EnumRepoWriteOperation.PR_CLOSE
    assert completed.mode is EnumRepoWriteMode.DRY_RUN


async def test_check_run_create_is_published_as_completed() -> None:
    output = {"title": "Lab job", "summary": "Recorded lab check."}
    post = ModelGithubHttpRequest(
        method="POST",
        path=f"/repos/{_REPO}/check-runs",
        body={
            "name": "lab/pr-merged-publisher",
            "head_sha": _HEAD,
            "status": "completed",
            "conclusion": "success",
            "output": output,
        },
    )
    fake = _Recorded(
        [
            (
                post,
                ModelGithubHttpResponse(
                    status=201,
                    headers=_HEADERS,
                    body={
                        "id": 4,
                        "head_sha": _HEAD,
                        "html_url": f"https://github.com/{_REPO}/runs/4",
                    },
                ),
            )
        ]
    )
    handler = HandlerGithubRepoWriteEffect(fake, clock=_CLOCK.timestamp)
    broker, correlation_id = await _run(
        handler,
        _command(
            EnumRepoWriteOperation.CHECK_RUN_CREATE,
            name="lab/pr-merged-publisher",
            head_sha=_HEAD,
            status="completed",
            conclusion="success",
            output=output,
        ),
    )
    (envelope,) = _on(broker, _COMPLETED)
    assert envelope.correlation_id == correlation_id
    completed = ModelRepoWriteCompleted.model_validate(envelope.payload)
    assert completed.operation is EnumRepoWriteOperation.CHECK_RUN_CREATE
    assert completed.check_run_id == 4
    assert completed.head_sha == _HEAD
    assert completed.html_url == f"https://github.com/{_REPO}/runs/4"
    assert completed.http_statuses == (201,)
    assert completed.mode is EnumRepoWriteMode.ENFORCE
    assert fake.sent == [post]
    assert fake.exchanges == []
    assert _on(broker, _FAILED) == []


def _overlay(*allowed: str) -> ModelRepoWriteOverlay:
    return ModelRepoWriteOverlay(
        pr_label=ModelPrLabelOverlay(allowed_labels=tuple(allowed))
    )


async def test_a_label_add_is_published_as_completed() -> None:
    read = pull_request_request(_REPO, 4000)
    add = add_labels_request(_REPO, 4000, [_LABEL])
    pr = {
        "number": 4000,
        "html_url": _PR_URL,
        "head": {"ref": "branch", "sha": _HEAD},
        "labels": [{"name": "kind:lab"}],
    }
    fake = _Recorded(
        [
            (read, ModelGithubHttpResponse(status=200, headers=_HEADERS, body=pr)),
            (
                add,
                ModelGithubHttpResponse(
                    status=200, headers=_HEADERS, body={"value": [{"name": _LABEL}]}
                ),
            ),
        ]
    )
    handler = HandlerGithubRepoWriteEffect(
        fake, clock=_CLOCK.timestamp, overlay=_overlay(_LABEL)
    )
    broker, correlation_id = await _run(
        handler,
        _command(
            EnumRepoWriteOperation.PR_LABEL,
            pr_number=4000,
            expected_head_sha=_HEAD,
            add_labels=[_LABEL],
        ),
    )
    (envelope,) = _on(broker, _COMPLETED)
    assert envelope.correlation_id == correlation_id
    completed = ModelRepoWriteCompleted.model_validate(envelope.payload)
    assert completed.operation is EnumRepoWriteOperation.PR_LABEL
    assert completed.mode is EnumRepoWriteMode.ENFORCE
    assert completed.replayed is False
    assert completed.html_url == _PR_URL
    assert completed.head_sha == _HEAD
    assert completed.requests == (read, add)
    assert completed.http_statuses == (200, 200)
    assert fake.sent == [read, add]
    assert fake.exchanges == []
    assert _on(broker, _FAILED) == []


async def test_a_label_outside_the_overlay_is_published_as_failed() -> None:
    fake = _Recorded([])
    handler = HandlerGithubRepoWriteEffect(
        fake, clock=_CLOCK.timestamp, overlay=_overlay(_LABEL)
    )
    broker, _ = await _run(
        handler,
        _command(
            EnumRepoWriteOperation.PR_LABEL,
            pr_number=4000,
            expected_head_sha=_HEAD,
            add_labels=[_OFF_LIMITS],
        ),
    )
    assert fake.sent == []
    assert fake.exchanges == []
    assert _on(broker, _COMPLETED) == []
    (envelope,) = _on(broker, _FAILED)
    failed = ModelRepoWriteFailed.model_validate(envelope.payload)
    assert failed.reason is EnumRepoWriteRefusal.LABEL_NOT_ALLOWED
    assert failed.terminal_failure_cause is EnumRepoWriteRefusal.LABEL_NOT_ALLOWED
    assert _OFF_LIMITS in failed.detail
    assert _LABEL not in failed.detail


async def test_a_label_write_on_a_moved_head_is_published_as_failed() -> None:
    moved = {
        "number": 4000,
        "html_url": _PR_URL,
        "head": {"ref": "branch", "sha": _MOVED},
        "labels": [],
    }
    read = pull_request_request(_REPO, 4000)
    fake = _Recorded(
        [(read, ModelGithubHttpResponse(status=200, headers=_HEADERS, body=moved))]
    )
    handler = HandlerGithubRepoWriteEffect(
        fake, clock=_CLOCK.timestamp, overlay=_overlay(_LABEL)
    )
    broker, _ = await _run(
        handler,
        _command(
            EnumRepoWriteOperation.PR_LABEL,
            pr_number=4000,
            expected_head_sha=_HEAD,
            add_labels=[_LABEL],
        ),
    )
    assert fake.sent == [read]
    assert fake.exchanges == []
    assert _on(broker, _COMPLETED) == []
    (envelope,) = _on(broker, _FAILED)
    failed = ModelRepoWriteFailed.model_validate(envelope.payload)
    assert failed.reason is EnumRepoWriteRefusal.HEAD_MOVED
    assert failed.terminal_failure_cause is EnumRepoWriteRefusal.HEAD_MOVED


def test_the_contract_declares_pr_label() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    entries = {entry["name"]: entry for entry in contract["operations"]}
    assert entries["pr_label"]["transport"] == "rest"
    assert (
        "replaces gh api --method POST /repos/{o}/{r}/issues/{n}/labels."
        in entries["pr_label"]["description"]
    )


# Test-local variable names and value only; no real variable name or value
# lives in src, the contract or the shipped overlay example.
_VAR = "LAB_VAR_ONE"
_VAR_OFF_LIMITS = "LAB_VAR_TWO"
_VAR_MARKED = "LAB_TOKEN_ONE"
_VAR_VALUE = "lab-one-value"


def _variables_overlay() -> ModelRepoWriteOverlay:
    return ModelRepoWriteOverlay(
        pr_label=ModelPrLabelOverlay(allowed_labels=(_LABEL,)),
        repo_variables=ModelRepoVariablesOverlay(
            entries=(
                ModelRepoVariablesEntry(
                    repository=_REPO, name=_VAR, source="lab/vars/one.txt"
                ),
            ),
            refuse_name_markers=("TOKEN",),
            refuse_value_markers=("LABSECRET",),
        ),
    )


def _variable_handler(fake: _Recorded, tmp_path: Path) -> HandlerGithubRepoWriteEffect:
    return HandlerGithubRepoWriteEffect(
        fake,
        clock=_CLOCK.timestamp,
        overlay=_variables_overlay(),
        variable_source_root=tmp_path,
    )


def _write_source(tmp_path: Path, text: str) -> None:
    path = tmp_path / "lab" / "vars" / "one.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{text}\n", encoding="utf-8")


async def test_a_repo_variable_set_is_published_as_completed(tmp_path: Path) -> None:
    _write_source(tmp_path, _VAR_VALUE)
    get = get_variable_request(_REPO, _VAR)
    create = create_variable_request(_REPO, _VAR, _VAR_VALUE)
    fake = _Recorded(
        [
            (
                get,
                ModelGithubHttpResponse(
                    status=404, headers=_HEADERS, body={"message": "Not Found"}
                ),
            ),
            (
                create,
                ModelGithubHttpResponse(
                    status=201, headers=_HEADERS, body={"name": _VAR}
                ),
            ),
        ]
    )
    handler = _variable_handler(fake, tmp_path)
    broker, correlation_id = await _run(
        handler,
        _command(EnumRepoWriteOperation.REPO_VARIABLE_SET, variable_name=_VAR),
    )
    (envelope,) = _on(broker, _COMPLETED)
    assert envelope.correlation_id == correlation_id
    completed = ModelRepoWriteCompleted.model_validate(envelope.payload)
    assert completed.operation is EnumRepoWriteOperation.REPO_VARIABLE_SET
    assert completed.mode is EnumRepoWriteMode.ENFORCE
    assert completed.replayed is False
    assert completed.variable_name == _VAR
    assert (
        completed.variable_digest
        == hashlib.sha256(_VAR_VALUE.encode("utf-8")).hexdigest()
    )
    assert completed.requests == (get, create)
    assert completed.http_statuses == (404, 201)
    assert fake.sent == [get, create]
    assert fake.exchanges == []
    assert _on(broker, _FAILED) == []


async def test_a_repo_variable_get_is_published_as_completed(tmp_path: Path) -> None:
    _write_source(tmp_path, _VAR_VALUE)
    get = get_variable_request(_REPO, _VAR)
    fake = _Recorded(
        [
            (
                get,
                ModelGithubHttpResponse(
                    status=200,
                    headers=_HEADERS,
                    body={"name": _VAR, "value": _VAR_VALUE},
                ),
            )
        ]
    )
    handler = _variable_handler(fake, tmp_path)
    broker, correlation_id = await _run(
        handler,
        _command(EnumRepoWriteOperation.REPO_VARIABLE_GET, variable_name=_VAR),
    )
    (envelope,) = _on(broker, _COMPLETED)
    assert envelope.correlation_id == correlation_id
    completed = ModelRepoWriteCompleted.model_validate(envelope.payload)
    assert completed.operation is EnumRepoWriteOperation.REPO_VARIABLE_GET
    assert completed.mode is EnumRepoWriteMode.ENFORCE
    assert completed.variable_name == _VAR
    assert completed.variable_value == _VAR_VALUE
    assert completed.variable_in_sync is True
    assert (
        completed.variable_digest
        == hashlib.sha256(_VAR_VALUE.encode("utf-8")).hexdigest()
    )
    assert completed.requests == (get,)
    assert completed.http_statuses == (200,)
    assert fake.sent == [get]
    assert fake.exchanges == []
    assert _on(broker, _FAILED) == []


async def test_a_variable_outside_the_overlay_is_published_as_failed(
    tmp_path: Path,
) -> None:
    fake = _Recorded([])
    handler = _variable_handler(fake, tmp_path)
    broker, _ = await _run(
        handler,
        _command(
            EnumRepoWriteOperation.REPO_VARIABLE_SET,
            variable_name=_VAR_OFF_LIMITS,
        ),
    )
    assert fake.sent == []
    assert fake.exchanges == []
    assert _on(broker, _COMPLETED) == []
    (envelope,) = _on(broker, _FAILED)
    failed = ModelRepoWriteFailed.model_validate(envelope.payload)
    assert failed.reason is EnumRepoWriteRefusal.VARIABLE_NOT_ALLOWED
    assert failed.terminal_failure_cause is EnumRepoWriteRefusal.VARIABLE_NOT_ALLOWED
    assert _VAR_OFF_LIMITS in failed.detail


async def test_a_variable_name_carrying_a_refuse_marker_is_published_as_failed(
    tmp_path: Path,
) -> None:
    fake = _Recorded([])
    handler = _variable_handler(fake, tmp_path)
    broker, _ = await _run(
        handler,
        _command(EnumRepoWriteOperation.REPO_VARIABLE_SET, variable_name=_VAR_MARKED),
    )
    assert fake.sent == []
    assert fake.exchanges == []
    assert _on(broker, _COMPLETED) == []
    (envelope,) = _on(broker, _FAILED)
    failed = ModelRepoWriteFailed.model_validate(envelope.payload)
    assert failed.reason is EnumRepoWriteRefusal.SECRET_REFUSED
    assert failed.terminal_failure_cause is EnumRepoWriteRefusal.SECRET_REFUSED
    assert "carries a refuse marker" in failed.detail


async def test_a_refused_variable_post_is_published_as_failed(tmp_path: Path) -> None:
    _write_source(tmp_path, _VAR_VALUE)
    get = get_variable_request(_REPO, _VAR)
    create = create_variable_request(_REPO, _VAR, _VAR_VALUE)
    fake = _Recorded(
        [
            (
                get,
                ModelGithubHttpResponse(
                    status=404, headers=_HEADERS, body={"message": "Not Found"}
                ),
            ),
            (
                create,
                ModelGithubHttpResponse(
                    status=403,
                    headers=_HEADERS,
                    body={"message": "Resource not accessible"},
                ),
            ),
        ]
    )
    handler = _variable_handler(fake, tmp_path)
    broker, _ = await _run(
        handler,
        _command(EnumRepoWriteOperation.REPO_VARIABLE_SET, variable_name=_VAR),
    )
    assert fake.sent == [get, create]
    assert fake.exchanges == []
    assert _on(broker, _COMPLETED) == []
    (envelope,) = _on(broker, _FAILED)
    failed = ModelRepoWriteFailed.model_validate(envelope.payload)
    assert failed.reason is EnumRepoWriteRefusal.GITHUB_REFUSED
    assert failed.terminal_failure_cause is EnumRepoWriteRefusal.GITHUB_REFUSED
    assert failed.http_status == 403
    assert failed.requests == (get, create)
    assert failed.http_statuses == (404, 403)


def test_the_contract_declares_repo_variable_operations() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    entries = {entry["name"]: entry for entry in contract["operations"]}
    for name, replaces in (
        ("repo_variable_set", "replaces gh variable set."),
        ("repo_variable_get", "replaces gh variable get."),
    ):
        assert entries[name]["transport"] == "rest"
        assert replaces in entries[name]["description"]


# Test-local organization and runner names only; the real names live in the
# overlays repository, never in src, the contract or the shipped overlay example.
_RUNNER_REPO = _REPO
_RUNNER_ORG = _REPO.split("/")[0]
_RUNNER = "example-runner-1"
_RUNNER_OFF_LIMITS = "example-runner-off-limits"


def _runners_overlay() -> ModelRepoWriteOverlay:
    return ModelRepoWriteOverlay(
        pr_label=ModelPrLabelOverlay(allowed_labels=(_LABEL,)),
        runner_removals=ModelRunnerRemovalOverlay(
            entries=(ModelRunnerEntry(organization=_RUNNER_ORG, name=_RUNNER),)
        ),
    )


def _runner_command(
    operation: EnumRepoWriteOperation, **kw: object
) -> ModelRepoWriteRequest:
    return ModelRepoWriteRequest.model_validate(
        {
            "correlation_id": uuid4(),
            "operation": operation,
            "repo": _RUNNER_REPO,
            "idempotency_key": "runner-chain-0001",
            "requested_by_lane": "author-lane",
            **kw,
        }
    )


async def test_a_runner_list_is_published_as_completed() -> None:
    page = list_runners_request(_RUNNER_ORG, 1)
    body = {
        "total_count": 1,
        "runners": [{"id": 9, "name": _RUNNER, "status": "offline", "busy": False}],
    }
    fake = _Recorded(
        [(page, ModelGithubHttpResponse(status=200, headers=_HEADERS, body=body))]
    )
    handler = HandlerGithubRepoWriteEffect(
        fake, clock=_CLOCK.timestamp, overlay=_runners_overlay()
    )
    broker, correlation_id = await _run(
        handler, _runner_command(EnumRepoWriteOperation.RUNNER_LIST)
    )
    (envelope,) = _on(broker, _COMPLETED)
    assert envelope.correlation_id == correlation_id
    completed = ModelRepoWriteCompleted.model_validate(envelope.payload)
    assert completed.operation is EnumRepoWriteOperation.RUNNER_LIST
    assert completed.mode is EnumRepoWriteMode.ENFORCE
    assert completed.runners is not None
    assert [(r.id, r.name, r.status, r.busy) for r in completed.runners] == [
        (9, _RUNNER, "offline", False)
    ]
    assert completed.requests == (page,)
    assert fake.sent == [page]
    assert fake.exchanges == []
    assert _on(broker, _FAILED) == []


async def test_a_runner_outside_the_overlay_is_published_as_failed() -> None:
    fake = _Recorded([])
    handler = HandlerGithubRepoWriteEffect(
        fake, clock=_CLOCK.timestamp, overlay=_runners_overlay()
    )
    broker, _ = await _run(
        handler,
        _runner_command(
            EnumRepoWriteOperation.RUNNER_REMOVE, runner_name=_RUNNER_OFF_LIMITS
        ),
    )
    assert fake.sent == []
    assert _on(broker, _COMPLETED) == []
    (envelope,) = _on(broker, _FAILED)
    failed = ModelRepoWriteFailed.model_validate(envelope.payload)
    assert failed.reason is EnumRepoWriteRefusal.RUNNER_NOT_ALLOWED
    assert failed.terminal_failure_cause is EnumRepoWriteRefusal.RUNNER_NOT_ALLOWED
    assert _RUNNER_OFF_LIMITS in failed.detail


async def test_an_online_runner_is_published_as_failed_with_no_delete() -> None:
    page = list_runners_request(_RUNNER_ORG, 1)
    body = {
        "total_count": 1,
        "runners": [{"id": 9, "name": _RUNNER, "status": "online", "busy": False}],
    }
    fake = _Recorded(
        [(page, ModelGithubHttpResponse(status=200, headers=_HEADERS, body=body))]
    )
    handler = HandlerGithubRepoWriteEffect(
        fake, clock=_CLOCK.timestamp, overlay=_runners_overlay()
    )
    broker, _ = await _run(
        handler,
        _runner_command(EnumRepoWriteOperation.RUNNER_REMOVE, runner_name=_RUNNER),
    )
    assert fake.sent == [page]
    assert _on(broker, _COMPLETED) == []
    (envelope,) = _on(broker, _FAILED)
    failed = ModelRepoWriteFailed.model_validate(envelope.payload)
    assert failed.reason is EnumRepoWriteRefusal.RUNNER_ONLINE
    assert failed.terminal_failure_cause is EnumRepoWriteRefusal.RUNNER_ONLINE


async def test_a_runner_group_get_is_published_as_completed() -> None:
    group_body = {
        "id": 7,
        "name": "example-group",
        "allows_public_repositories": False,
        "restricted_to_selected_workflows": True,
        "selected_workflows": [
            "example-org/example-repo/.github/workflows/build.yml@refs/heads/main"
        ],
    }
    group_req = get_runner_group_request(_RUNNER_ORG, 7)
    repos_req = list_runner_group_repositories_request(_RUNNER_ORG, 7, 1)
    repos_body = {
        "total_count": 1,
        "repositories": [{"full_name": "example-org/example-repo"}],
    }
    fake = _Recorded(
        [
            (
                group_req,
                ModelGithubHttpResponse(status=200, headers=_HEADERS, body=group_body),
            ),
            (
                repos_req,
                ModelGithubHttpResponse(status=200, headers=_HEADERS, body=repos_body),
            ),
        ]
    )
    handler = HandlerGithubRepoWriteEffect(
        fake,
        clock=_CLOCK.timestamp,
        overlay=ModelRepoWriteOverlay(
            pr_label=ModelPrLabelOverlay(allowed_labels=(_LABEL,)),
            runner_group_reads=ModelRunnerGroupReadOverlay(
                entries=(ModelRunnerGroupEntry(organization=_RUNNER_ORG, group_id=7),)
            ),
        ),
    )
    broker, correlation_id = await _run(
        handler,
        _runner_command(EnumRepoWriteOperation.RUNNER_GROUP_GET, runner_group_id=7),
    )
    (envelope,) = _on(broker, _COMPLETED)
    assert envelope.correlation_id == correlation_id
    completed = ModelRepoWriteCompleted.model_validate(envelope.payload)
    assert completed.operation is EnumRepoWriteOperation.RUNNER_GROUP_GET
    assert completed.mode is EnumRepoWriteMode.ENFORCE
    assert completed.runner_group is not None
    assert completed.runner_group.id == 7
    assert completed.runner_group.name == "example-group"
    assert completed.runner_group.allows_public_repositories is False
    assert completed.runner_group.restricted_to_selected_workflows is True
    assert completed.runner_group.selected_workflows == (
        "example-org/example-repo/.github/workflows/build.yml@refs/heads/main",
    )
    assert completed.runner_group.repositories == ("example-org/example-repo",)
    assert completed.requests == (group_req, repos_req)
    assert fake.sent == [group_req, repos_req]
    assert fake.exchanges == []
    assert _on(broker, _FAILED) == []


async def test_a_runner_group_outside_the_overlay_is_published_as_failed() -> None:
    fake = _Recorded([])
    handler = HandlerGithubRepoWriteEffect(
        fake,
        clock=_CLOCK.timestamp,
        overlay=ModelRepoWriteOverlay(
            pr_label=ModelPrLabelOverlay(allowed_labels=(_LABEL,)),
            runner_group_reads=ModelRunnerGroupReadOverlay(
                entries=(ModelRunnerGroupEntry(organization=_RUNNER_ORG, group_id=7),)
            ),
        ),
    )
    broker, _ = await _run(
        handler,
        _runner_command(EnumRepoWriteOperation.RUNNER_GROUP_GET, runner_group_id=9),
    )
    assert fake.sent == []
    assert _on(broker, _COMPLETED) == []
    (envelope,) = _on(broker, _FAILED)
    failed = ModelRepoWriteFailed.model_validate(envelope.payload)
    assert failed.reason is EnumRepoWriteRefusal.RUNNER_NOT_ALLOWED
    assert failed.terminal_failure_cause is EnumRepoWriteRefusal.RUNNER_NOT_ALLOWED


def test_the_contract_declares_the_runner_operations() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    entries = {entry["name"]: entry for entry in contract["operations"]}
    for name, replaces in (
        ("runner_list", "replaces gh api --method GET /orgs/{o}/actions/runners."),
        (
            "runner_remove",
            "replaces gh api --method DELETE /orgs/{o}/actions/runners/{id}.",
        ),
        (
            "runner_group_get",
            "replaces gh api --method GET /orgs/{o}/actions/runner-groups/{id}.",
        ),
    ):
        assert entries[name]["transport"] == "rest"
        assert replaces in entries[name]["description"]
