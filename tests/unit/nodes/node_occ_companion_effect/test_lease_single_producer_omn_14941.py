# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14941: single-producer lease wired live in the canonical write-EFFECT.

The handler's OMN-14793 note promised the lease "when promoted to LIVE
authority"; the born path (OMN-14941) is that promotion. These tests prove:

* the mutate path acquires ``acquire_occ_companion_lease`` keyed on
  repo + PR number + product head SHA BEFORE any clone/push;
* a second concurrent producer (acquire returns ``False``) skips with ZERO
  git/gh side effects (the OCC#4406 dual-producer race);
* the lease is released in a ``finally`` on both success-path failure and
  mid-mint crash, and is NOT released when it was never acquired (the other
  producer owns it).

The RSD-2 read is stubbed (same pattern as the dry-run tests) and every
git/REST surface is a tripwire, so the tests run fully offline.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
)
from omnimarket.nodes.node_occ_companion_effect.handlers import (
    handler_occ_companion_effect as effect_mod,
)
from omnimarket.nodes.node_occ_companion_effect.handlers.handler_occ_companion_effect import (
    HandlerOccCompanionEffect,
)
from omnimarket.nodes.node_occ_companion_effect.models.model_occ_companion_effect_request import (
    ModelOccCompanionEffectRequest,
)
from omnimarket.nodes.node_occ_state_effect.handlers.handler_occ_state_effect import (
    HandlerOccStateEffect,
)
from omnimarket.nodes.node_occ_state_effect.models.model_occ_state_request import (
    ModelOccStateRequest,
)

_HEAD_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


class _StubStateHandler(HandlerOccStateEffect):
    def __init__(self, request: ModelOccCompanionRequest) -> None:
        self._request = request

    async def handle(self, request: ModelOccStateRequest) -> ModelOccCompanionRequest:
        return self._request


def _canned_request() -> ModelOccCompanionRequest:
    return ModelOccCompanionRequest(
        repo="OmniNode-ai/omnimarket",
        pr_number=1760,
        pr_head_sha=_HEAD_SHA,
        pr_title="feat(OMN-14608): thing",
        pr_body="Closes OMN-14608",
        run_timestamp="2026-07-14T12:00:00Z",
        product_probe=ModelObservedProbe(
            command="gh pr view 1760",
            stdout='{"number":1760,"state":"OPEN"}',
            exit_code=0,
        ),
    )


def _mutate_request() -> ModelOccCompanionEffectRequest:
    return ModelOccCompanionEffectRequest(
        repo="OmniNode-ai/omnimarket", pr_number=1760, mode="mutate"
    )


class _LeaseRecorder:
    """Records acquire/release calls; acquire returns a scripted verdict."""

    def __init__(self, acquire_result: bool) -> None:
        self._acquire_result = acquire_result
        self.acquire_calls: list[dict[str, object]] = []
        self.release_calls: list[dict[str, object]] = []

    def acquire(self, **kwargs: object) -> bool:
        self.acquire_calls.append(kwargs)
        return self._acquire_result

    def release(self, **kwargs: object) -> None:
        self.release_calls.append(kwargs)


def _wire_offline(monkeypatch: pytest.MonkeyPatch, lease: _LeaseRecorder) -> None:
    """Stub the token + lease surfaces and turn every mutation into a tripwire."""
    monkeypatch.setattr(effect_mod, "_resolve_github_token", lambda: "t")
    monkeypatch.setattr(effect_mod, "acquire_occ_companion_lease", lease.acquire)
    monkeypatch.setattr(effect_mod, "release_occ_companion_lease", lease.release)


def _install_mutation_tripwires(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: object, **k: object) -> object:
        raise AssertionError("a lease-held skip must perform ZERO git/gh side effects")

    monkeypatch.setattr(effect_mod, "run_git", _boom)
    monkeypatch.setattr(effect_mod, "rest_json", _boom)
    monkeypatch.setattr(effect_mod, "rest_json_array", _boom)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lease_held_second_producer_skips_with_zero_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _LeaseRecorder(acquire_result=False)
    _wire_offline(monkeypatch, lease)
    _install_mutation_tripwires(monkeypatch)

    handler = HandlerOccCompanionEffect(
        state_handler=_StubStateHandler(_canned_request())
    )
    result = await handler.handle(_mutate_request())

    assert "LEASE_HELD" in result.action
    assert result.occ_pr_number is None
    assert result.product_body_stamped is False
    # Never released: the OTHER producer owns the lease; releasing it here would
    # yank a live mint's lock out from under it.
    assert lease.release_calls == []
    assert len(lease.acquire_calls) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lease_acquired_before_any_clone_and_keyed_on_repo_pr_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _LeaseRecorder(acquire_result=False)
    _wire_offline(monkeypatch, lease)
    _install_mutation_tripwires(monkeypatch)

    handler = HandlerOccCompanionEffect(
        state_handler=_StubStateHandler(_canned_request())
    )
    await handler.handle(_mutate_request())

    (call,) = lease.acquire_calls
    assert call["repo_slug"] == "OmniNode-ai/omnimarket"
    assert call["pr_number"] == 1760
    # Keyed on the PRODUCT head SHA from the RSD-2 read (companion_request),
    # so both producers contend on the same key regardless of host.
    assert call["head_sha"] == _HEAD_SHA
    assert call["occ_repo"] == "OmniNode-ai/onex_change_control"
    assert str(call["producer_id"]).startswith("node_occ_companion_effect@")
    assert call["lease_ttl_seconds"] == effect_mod._LEASE_TTL_SECONDS


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lease_released_in_finally_when_the_mint_crashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-mint failure (clone explodes) must still release the lease so the
    head is freed immediately — the TTL steal is only the hard-kill backstop."""
    lease = _LeaseRecorder(acquire_result=True)
    _wire_offline(monkeypatch, lease)

    def _clone_boom(*a: object, **k: object) -> object:
        raise RuntimeError("clone boom")

    monkeypatch.setattr(effect_mod, "run_git", _clone_boom)

    handler = HandlerOccCompanionEffect(
        state_handler=_StubStateHandler(_canned_request())
    )
    with pytest.raises(RuntimeError, match="clone boom"):
        await handler.handle(_mutate_request())

    (release,) = lease.release_calls
    assert release["repo_slug"] == "OmniNode-ai/omnimarket"
    assert release["pr_number"] == 1760
    assert release["head_sha"] == _HEAD_SHA
    assert release["occ_repo"] == "OmniNode-ai/onex_change_control"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_never_touches_the_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fail-safe default stays lease-free: dry_run does read+compute only."""
    lease = _LeaseRecorder(acquire_result=True)
    _wire_offline(monkeypatch, lease)
    _install_mutation_tripwires(monkeypatch)

    handler = HandlerOccCompanionEffect(
        state_handler=_StubStateHandler(_canned_request())
    )
    result = await handler.handle(
        ModelOccCompanionEffectRequest(repo="OmniNode-ai/omnimarket", pr_number=1760)
    )
    assert result.mode == "dry_run"
    assert lease.acquire_calls == []
    assert lease.release_calls == []
