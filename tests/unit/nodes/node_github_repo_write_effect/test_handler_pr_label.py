# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""pr_label on a recorded fake transport (OMN-23821).

Each scenario replays a strict, ordered list of exchanges, so the request shapes
are pinned. The planted cases prove the refusals: a label outside the overlay's
allowed set sends nothing, a moved head sends no mutation, and a label already in
the asked-for state is not written again. The label names here are test-local; the
shipped overlay example and the node's source carry none.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.github_landing.github_landing_requests import pull_request_request
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.nodes.node_github_repo_write_effect.handlers.handler_github_repo_write import (
    HandlerGithubRepoWriteEffect,
    planned_requests,
)
from omnimarket.nodes.node_github_repo_write_effect.models import (
    EnumRepoWriteMode,
    EnumRepoWriteOperation,
    EnumRepoWriteRefusal,
    ModelRepoWriteCompleted,
    ModelRepoWriteConfig,
    ModelRepoWriteFailed,
    ModelRepoWriteRequest,
)
from omnimarket.nodes.node_github_repo_write_effect.overlay import (
    ModelPrLabelOverlay,
    ModelRepoWriteOverlay,
)
from omnimarket.nodes.node_github_repo_write_effect.services.pr_label import (
    add_labels_request,
    remove_label_request,
)

pytestmark = pytest.mark.unit

_NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_github_repo_write_effect"
)
_REPO = "OmniNode-ai/omnimarket"
_PR = 4000
_KEY = "label-key-0001"
_HEAD = "4ceedeafeb4040c987883d96c20e745ad5eed6e1"
_MOVED = "a7f20d7a0" + "0" * 31
_NOW = 1_791_000_000.0
_CORRELATION = UUID("00000000-0000-4000-8000-000000000003")
_READY = "lab:ready"
_HOLD = "lab:hold"
_OFF_LIMITS = "lab:off-limits"
_PR_URL = f"https://github.com/{_REPO}/pull/{_PR}"


def _headers() -> dict[str, str]:
    return {
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": "4900",
        "x-ratelimit-used": "100",
        "x-ratelimit-reset": str(int(_NOW) + 3600),
        "x-ratelimit-resource": "core",
    }


def _ok(body: dict[str, Any]) -> ModelGithubHttpResponse:
    return ModelGithubHttpResponse(status=200, headers=_headers(), body=body)


def _pr(head: str = _HEAD, labels: tuple[str, ...] = ()) -> ModelGithubHttpResponse:
    return _ok(
        {
            "number": _PR,
            "html_url": _PR_URL,
            "draft": True,
            "head": {"ref": "branch", "sha": head},
            "labels": [{"name": name} for name in labels],
        }
    )


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


class _NoClaims:
    def active_claim(self, *, claims_dir: str, pr_key: str, now: str) -> None:
        return None


def _overlay(*allowed: str) -> ModelRepoWriteOverlay:
    return ModelRepoWriteOverlay(
        pr_label=ModelPrLabelOverlay(allowed_labels=tuple(allowed))
    )


def _handler(
    transport: _Recorded, overlay: ModelRepoWriteOverlay | None = None
) -> HandlerGithubRepoWriteEffect:
    return HandlerGithubRepoWriteEffect(
        transport,
        claim_lookup=_NoClaims(),
        runtime_claims_dir="",
        config=ModelRepoWriteConfig(
            github_mode=EnumRepoWriteMode.DRY_RUN,
            github_mode_by_repository={_REPO: EnumRepoWriteMode.ENFORCE},
        ),
        clock=lambda: _NOW,
        overlay=overlay if overlay is not None else _overlay(_READY, _HOLD),
    )


def _label(**kw: object) -> ModelRepoWriteRequest:
    return ModelRepoWriteRequest.model_validate(
        {
            "correlation_id": _CORRELATION,
            "operation": EnumRepoWriteOperation.PR_LABEL,
            "repo": _REPO,
            "idempotency_key": _KEY,
            "requested_by_lane": "author-lane",
            "pr_number": _PR,
            "expected_head_sha": _HEAD,
            **kw,
        }
    )


_READ = pull_request_request(_REPO, _PR)


async def test_a_label_is_added_to_a_draft_pull_request() -> None:
    add = add_labels_request(_REPO, _PR, [_READY])
    transport = _Recorded([(_READ, _pr()), (add, _ok({"value": [{"name": _READY}]}))])
    result = await _handler(transport).handle(_label(add_labels=[_READY]))
    assert isinstance(result, ModelRepoWriteCompleted)
    assert (result.replayed, result.mode) == (False, EnumRepoWriteMode.ENFORCE)
    assert result.head_sha == _HEAD
    assert result.html_url == _PR_URL
    assert transport.sent == [_READ, add]
    assert add.method == "POST"
    assert add.path == f"/repos/{_REPO}/issues/{_PR}/labels"
    assert add.body == {"labels": [_READY]}
    assert not transport.exchanges


async def test_a_label_is_removed_from_a_pull_request() -> None:
    remove = remove_label_request(_REPO, _PR, _READY)
    transport = _Recorded(
        [(_READ, _pr(labels=(_READY,))), (remove, _ok({"value": []}))]
    )
    result = await _handler(transport).handle(_label(remove_labels=[_READY]))
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.replayed is False
    assert transport.sent == [_READ, remove]
    assert remove.method == "DELETE"
    assert remove.path == f"/repos/{_REPO}/issues/{_PR}/labels/lab%3Aready"
    assert remove.body is None


async def test_add_and_remove_in_one_command_write_each_once() -> None:
    add = add_labels_request(_REPO, _PR, [_READY])
    remove = remove_label_request(_REPO, _PR, _HOLD)
    transport = _Recorded(
        [
            (_READ, _pr(labels=(_HOLD,))),
            (add, _ok({"value": []})),
            (remove, _ok({"value": []})),
        ]
    )
    result = await _handler(transport).handle(
        _label(add_labels=[_READY], remove_labels=[_HOLD])
    )
    assert isinstance(result, ModelRepoWriteCompleted)
    assert transport.sent == [_READ, add, remove]
    assert not transport.exchanges


@pytest.mark.parametrize("field_name", ["add_labels", "remove_labels"])
async def test_a_label_outside_the_overlay_is_refused_with_no_call(
    field_name: str,
) -> None:
    transport = _Recorded([])
    result = await _handler(transport).handle(_label(**{field_name: [_OFF_LIMITS]}))
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.LABEL_NOT_ALLOWED
    assert _OFF_LIMITS in result.detail
    assert _READY not in result.detail
    assert transport.sent == []


async def test_one_label_outside_the_overlay_refuses_the_whole_command() -> None:
    transport = _Recorded([])
    result = await _handler(transport).handle(
        _label(add_labels=[_READY], remove_labels=[_OFF_LIMITS])
    )
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.LABEL_NOT_ALLOWED
    assert transport.sent == []


async def test_a_dry_run_still_refuses_a_label_outside_the_overlay() -> None:
    transport = _Recorded([])
    result = await _handler(transport).handle(
        _label(add_labels=[_OFF_LIMITS], dry_run=True)
    )
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.LABEL_NOT_ALLOWED


async def test_no_overlay_configured_refuses_every_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    transport = _Recorded([])
    handler = HandlerGithubRepoWriteEffect(
        transport,
        claim_lookup=_NoClaims(),
        runtime_claims_dir="",
        config=ModelRepoWriteConfig(
            github_mode_by_repository={_REPO: EnumRepoWriteMode.ENFORCE}
        ),
        clock=lambda: _NOW,
    )
    result = await handler.handle(_label(add_labels=[_READY]))
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.LABEL_NOT_ALLOWED
    assert "no allowed-label overlay is configured" in result.detail
    assert transport.sent == []


async def test_the_allowed_set_is_read_from_the_overlay_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    folder = tmp_path / "node_github_repo_write_effect"
    folder.mkdir()
    (folder / "overlay.yaml").write_text(
        yaml.safe_dump({"pr_label": {"allowed_labels": [_READY]}}), encoding="utf-8"
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    add = add_labels_request(_REPO, _PR, [_READY])
    transport = _Recorded([(_READ, _pr()), (add, _ok({"value": []}))])
    handler = HandlerGithubRepoWriteEffect(
        transport,
        claim_lookup=_NoClaims(),
        runtime_claims_dir="",
        config=ModelRepoWriteConfig(
            github_mode_by_repository={_REPO: EnumRepoWriteMode.ENFORCE}
        ),
        clock=lambda: _NOW,
    )
    assert isinstance(
        await handler.handle(_label(add_labels=[_READY])), ModelRepoWriteCompleted
    )
    refused = await handler.handle(
        _label(add_labels=[_HOLD], idempotency_key="label-key-0002")
    )
    assert isinstance(refused, ModelRepoWriteFailed)
    assert refused.reason is EnumRepoWriteRefusal.LABEL_NOT_ALLOWED


async def test_a_moved_head_is_refused_before_any_mutation() -> None:
    transport = _Recorded([(_READ, _pr(head=_MOVED))])
    result = await _handler(transport).handle(_label(add_labels=[_READY]))
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.HEAD_MOVED
    assert transport.sent == [_READ]


async def test_labels_already_in_the_asked_for_state_are_not_written() -> None:
    transport = _Recorded([(_READ, _pr(labels=(_READY,)))])
    result = await _handler(transport).handle(
        _label(add_labels=[_READY], remove_labels=[_HOLD])
    )
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.replayed is True
    assert transport.sent == [_READ]


async def test_a_repeated_idempotency_key_writes_once() -> None:
    add = add_labels_request(_REPO, _PR, [_READY])
    transport = _Recorded([(_READ, _pr()), (add, _ok({"value": []}))])
    handler = _handler(transport)
    first = await handler.handle(_label(add_labels=[_READY]))
    second = await handler.handle(_label(add_labels=[_READY]))
    assert isinstance(first, ModelRepoWriteCompleted)
    assert isinstance(second, ModelRepoWriteCompleted)
    assert (first.replayed, second.replayed) == (False, True)
    assert transport.sent == [_READ, add]


async def test_the_same_key_with_other_labels_is_a_conflict() -> None:
    add = add_labels_request(_REPO, _PR, [_READY])
    transport = _Recorded([(_READ, _pr()), (add, _ok({"value": []}))])
    handler = _handler(transport)
    await handler.handle(_label(add_labels=[_READY]))
    result = await handler.handle(_label(add_labels=[_HOLD]))
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.IDEMPOTENCY_KEY_CONFLICT
    assert "add_labels" in result.detail
    assert transport.sent == [_READ, add]


async def test_a_github_refusal_on_the_write_is_github_refused() -> None:
    add = add_labels_request(_REPO, _PR, [_READY])
    refused = ModelGithubHttpResponse(
        status=403, headers=_headers(), body={"message": "Resource not accessible"}
    )
    transport = _Recorded([(_READ, _pr()), (add, refused)])
    result = await _handler(transport).handle(_label(add_labels=[_READY]))
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.GITHUB_REFUSED


def test_the_dry_run_plan_is_the_read_then_the_writes() -> None:
    assert planned_requests(_label(add_labels=[_READY], remove_labels=[_HOLD])) == (
        _READ,
        add_labels_request(_REPO, _PR, [_READY]),
        remove_label_request(_REPO, _PR, _HOLD),
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"pr_number": None},
        {"expected_head_sha": None},
        {"add_labels": None},
        {"add_labels": []},
        {"add_labels": [""]},
        {"add_labels": ["bad\nlabel"]},
        {"add_labels": ["x" * 51]},
        {"remove_labels": [_READY]},
    ],
)
def test_the_request_names_the_pull_request_the_head_and_valid_labels(
    bad: dict[str, object],
) -> None:
    base = json.loads(_label(add_labels=[_READY]).model_dump_json())
    candidate = copy.deepcopy(base)
    candidate.update(bad)
    with pytest.raises(ValidationError):
        ModelRepoWriteRequest.model_validate(candidate)


def test_other_operations_do_not_carry_the_label_fields() -> None:
    comment = ModelRepoWriteRequest.model_validate(
        {
            "correlation_id": _CORRELATION,
            "operation": EnumRepoWriteOperation.PR_COMMENT,
            "repo": _REPO,
            "idempotency_key": _KEY,
            "requested_by_lane": "lane-x",
            "pr_number": _PR,
            "body": "hello",
        }
    )
    dumped = comment.model_dump(mode="json")
    assert "add_labels" not in dumped
    assert "remove_labels" not in dumped
    with pytest.raises(ValidationError):
        ModelRepoWriteRequest.model_validate({**dumped, "add_labels": [_READY]})


def test_the_shipped_overlay_example_loads() -> None:
    document = yaml.safe_load((_NODE / "overlay.example.yaml").read_text("utf-8"))
    overlay = ModelRepoWriteOverlay.model_validate(document)
    assert overlay.pr_label.allowed_labels


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"pr_label": {}},
        {"pr_label": {"allowed_labels": []}},
        {"pr_label": {"allowed_labels": [_READY, _READY]}},
        {"pr_label": {"allowed_labels": [" "]}},
        {"pr_label": {"allowed_labels": [_READY], "other": 1}},
    ],
)
def test_an_overlay_without_a_declared_label_set_refuses_the_load(
    document: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ModelRepoWriteOverlay.model_validate(document)


def test_no_label_value_is_written_in_the_node_source() -> None:
    names = [p for p in _NODE.rglob("*") if p.suffix in {".py", ".yaml"}]
    assert names
    assert [p.name for p in names if "ci:ready" in p.read_text("utf-8")] == []
