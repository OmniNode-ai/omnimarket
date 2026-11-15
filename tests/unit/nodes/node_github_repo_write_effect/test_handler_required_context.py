# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""required_context_apply behind the overlay's required_contexts allowlist.

No declared tool applied required status-check contexts, so the write node gets one,
served only for a (repository, branch, context) the overlay names. Each refusal is
proved to send nothing; the allowed triple runs on a recorded transport. Nothing here
applies a context to any repository: the transport is a fake.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

import pytest
from pydantic import ValidationError

from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.nodes.node_github_repo_write_effect.handlers.handler_github_repo_write import (
    HandlerGithubRepoWriteEffect,
)
from omnimarket.nodes.node_github_repo_write_effect.models import (
    EnumRepoWriteMode,
    EnumRepoWriteRefusal,
    ModelRepoWriteCompleted,
    ModelRepoWriteConfig,
    ModelRepoWriteFailed,
    ModelRepoWriteRequest,
)
from omnimarket.nodes.node_github_repo_write_effect.overlay import (
    ModelPrLabelOverlay,
    ModelRepoWriteOverlay,
    ModelRequiredContextEntry,
    ModelRequiredContextOverlay,
)
from omnimarket.nodes.node_github_repo_write_effect.services.required_context import (
    add_required_context_request,
    read_required_checks_request,
)

pytestmark = pytest.mark.unit

_REPO = "OmniNode-ai/omnimarket"
_BRANCH = "dev"
_CONTEXT = "fable-review-gate"
_NOW = 1_791_000_000.0
_CORRELATION = UUID("00000000-0000-4000-8000-00000000000a")


def _headers() -> dict[str, str]:
    return {
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": "4900",
        "x-ratelimit-used": "100",
        "x-ratelimit-reset": str(int(_NOW) + 3600),
        "x-ratelimit-resource": "core",
    }


def _ok(body: dict[str, object]) -> ModelGithubHttpResponse:
    return ModelGithubHttpResponse(status=200, headers=_headers(), body=body)


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


def _overlay(
    *entries: tuple[str, str, str], declared: bool = True
) -> ModelRepoWriteOverlay:
    return ModelRepoWriteOverlay(
        pr_label=ModelPrLabelOverlay(allowed_labels=("lab:ready",)),
        required_contexts=ModelRequiredContextOverlay(
            entries=tuple(
                ModelRequiredContextEntry(repository=r, branch=b, context=c)
                for r, b, c in entries
            )
        )
        if declared
        else None,
    )


def _handler(
    transport: _Recorded, overlay: ModelRepoWriteOverlay
) -> HandlerGithubRepoWriteEffect:
    return HandlerGithubRepoWriteEffect(
        transport,
        claim_lookup=_NoClaims(),
        runtime_claims_dir="",
        config=ModelRepoWriteConfig(
            github_mode=EnumRepoWriteMode.DRY_RUN,
            github_mode_by_repository={
                _REPO: EnumRepoWriteMode.ENFORCE,
                "OmniNode-ai/other": EnumRepoWriteMode.ENFORCE,
            },
        ),
        clock=lambda: _NOW,
        overlay=overlay,
    )


def _apply(**overrides: object) -> ModelRepoWriteRequest:
    return ModelRepoWriteRequest.model_validate(
        {
            "correlation_id": _CORRELATION,
            "operation": "required_context_apply",
            "repo": _REPO,
            "idempotency_key": "required-context-24638",
            "requested_by_lane": "gate-status-2226",
            "protected_branch": _BRANCH,
            "required_context": _CONTEXT,
            **overrides,
        }
    )


_ALLOWED = (_REPO, _BRANCH, _CONTEXT)


async def test_required_context_allowlist_adds_an_allowed_context_keeping_the_others() -> (
    None
):
    read = read_required_checks_request(_REPO, _BRANCH)
    existing = [{"context": "CI Summary", "app_id": 15368}]
    patch = add_required_context_request(
        _REPO, _BRANCH, [*existing, {"context": _CONTEXT}]
    )
    transport = _Recorded(
        [(read, _ok({"strict": True, "checks": existing})), (patch, _ok({}))]
    )
    result = await _handler(transport, _overlay(_ALLOWED)).handle(_apply())
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.required_context_applied is True
    assert transport.sent == [read, patch]
    assert patch.method == "PATCH"
    assert patch.path == (
        f"/repos/{_REPO}/branches/{_BRANCH}/protection/required_status_checks"
    )


async def test_required_context_allowlist_replays_when_already_required() -> None:
    read = read_required_checks_request(_REPO, _BRANCH)
    transport = _Recorded([(read, _ok({"checks": [{"context": _CONTEXT}]}))])
    result = await _handler(transport, _overlay(_ALLOWED)).handle(_apply())
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.replayed is True
    assert result.required_context_applied is False
    assert transport.sent == [read]


@pytest.mark.parametrize(
    "overrides",
    [
        {"repo": "OmniNode-ai/other"},
        {"protected_branch": "main"},
        {"required_context": "CI Summary"},
    ],
    ids=["repository", "branch", "context"],
)
async def test_required_context_allowlist_refuses_outside_the_allowlist(
    overrides: dict[str, object],
) -> None:
    transport = _Recorded([])
    result = await _handler(transport, _overlay(_ALLOWED)).handle(_apply(**overrides))
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.REQUIRED_CONTEXT_NOT_ALLOWED
    assert transport.sent == []


async def test_required_context_allowlist_refuses_when_the_overlay_declares_none() -> (
    None
):
    transport = _Recorded([])
    result = await _handler(transport, _overlay(declared=False)).handle(_apply())
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.REQUIRED_CONTEXT_NOT_ALLOWED
    assert transport.sent == []


async def test_required_context_allowlist_refuses_in_dry_run_too() -> None:
    transport = _Recorded([])
    result = await _handler(transport, _overlay(_ALLOWED)).handle(
        _apply(required_context="CI Summary", dry_run=True)
    )
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.REQUIRED_CONTEXT_NOT_ALLOWED


def test_required_context_allowlist_overlay_refuses_a_duplicate_entry() -> None:
    with pytest.raises(ValidationError, match="named twice"):
        _overlay(_ALLOWED, _ALLOWED)


def test_required_context_allowlist_requires_branch_and_context() -> None:
    with pytest.raises(ValidationError, match="protected_branch"):
        ModelRepoWriteRequest.model_validate(
            {
                "correlation_id": _CORRELATION,
                "operation": "required_context_apply",
                "repo": _REPO,
                "idempotency_key": "required-context-24638",
                "requested_by_lane": "gate-status-2226",
                "required_context": _CONTEXT,
            }
        )
