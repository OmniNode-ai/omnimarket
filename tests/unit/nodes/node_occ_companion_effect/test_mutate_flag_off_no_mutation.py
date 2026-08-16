# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deliverable 4(a): with the mutate flag OFF (dry_run default), a product PR

behaves byte-identically to today — the write-EFFECT performs ZERO GitHub
mutation. Also proves the OMN-14393 marker-seam label is (1) applied only on the
mutate write path and (2) best-effort (never aborts authoring).
"""

from __future__ import annotations

import pytest

from omnimarket.events.occ_autoauthor import OCC_MACHINE_MINTED_LABEL
from omnimarket.github_api import GitHubApiError
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


class _StubStateHandler(HandlerOccStateEffect):
    def __init__(self, request: ModelOccCompanionRequest) -> None:
        self._request = request

    async def handle(self, request: ModelOccStateRequest) -> ModelOccCompanionRequest:
        return self._request


class _MutationTripwireHandler(HandlerOccCompanionEffect):
    """Any call into the write/mutation surface raises — proving dry_run avoids it."""

    def _write_sync(self, *a: object, **k: object) -> object:  # type: ignore[override]
        raise AssertionError("dry_run must NOT reach the write path (_write_sync)")


def _canned_request() -> ModelOccCompanionRequest:
    return ModelOccCompanionRequest(
        repo="OmniNode-ai/omnimarket",
        pr_number=1760,
        pr_head_sha="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
        pr_title="feat(OMN-14608): thing",
        pr_body="Closes OMN-14608",
        run_timestamp="2026-07-14T12:00:00Z",
        product_probe=ModelObservedProbe(
            command="gh pr view 1760",
            stdout='{"number":1760,"state":"OPEN"}',
            exit_code=0,
        ),
    )


@pytest.mark.unit
def test_default_mode_is_dry_run() -> None:
    # The flag defaults OFF at the model boundary — no workflow variable can make
    # a bare request mutate.
    assert (
        ModelOccCompanionEffectRequest(repo="OmniNode-ai/omnimarket", pr_number=1).mode
        == "dry_run"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_never_touches_the_write_path() -> None:
    handler = _MutationTripwireHandler(
        state_handler=_StubStateHandler(_canned_request())
    )
    # Would raise AssertionError if the write path were reached.
    result = await handler.handle(
        ModelOccCompanionEffectRequest(repo="OmniNode-ai/omnimarket", pr_number=1760)
    )
    assert result.mode == "dry_run"
    assert result.occ_pr_number is None
    assert result.product_body_stamped is False
    # Deterministic plan was still computed (read+compute happened) — byte-identical
    # to today's dry_run/observe behavior.
    assert result.deterministic_digest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_does_no_github_mutation_even_if_apis_would_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Make every mutating REST call explode. dry_run must still succeed, proving it
    # issues NONE of them.
    def _boom(*a: object, **k: object) -> object:
        raise AssertionError("dry_run issued a GitHub REST call — it must not")

    monkeypatch.setattr(effect_mod, "rest_json", _boom)
    monkeypatch.setattr(effect_mod, "rest_json_array", _boom)
    monkeypatch.setattr(effect_mod, "run_git", _boom)

    handler = HandlerOccCompanionEffect(
        state_handler=_StubStateHandler(_canned_request())
    )
    result = await handler.handle(
        ModelOccCompanionEffectRequest(repo="OmniNode-ai/omnimarket", pr_number=1760)
    )
    assert result.mode == "dry_run"


@pytest.mark.unit
def test_marker_label_is_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    # A label API failure must NOT raise — the marker is observability, not a gate.
    calls: list[str] = []

    def _raise(method: str, path: str, **k: object) -> object:
        calls.append(path)
        raise GitHubApiError("boom")

    # OMN-15441: patches `rest_json_array`, the helper the handler now calls.
    # The labels endpoint returns a label ARRAY, which `rest_json`'s dict-only
    # contract rejected on every live call.
    monkeypatch.setattr(effect_mod, "rest_json_array", _raise)
    handler = HandlerOccCompanionEffect()
    # Must not raise despite the API error.
    handler._apply_machine_minted_label("OmniNode-ai", "onex_change_control", 4284, "t")
    assert len(calls) == 1
    assert "/labels" in calls[0]


@pytest.mark.unit
def test_marker_label_posts_the_machine_minted_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _capture(method: str, path: str, **k: object) -> list[dict[str, object]]:
        captured["method"] = method
        captured["path"] = path
        captured["body"] = k.get("body")
        # OMN-15441: return the REAL response shape (an array of label objects).
        # This stub previously returned `{}` through `rest_json`, which is why
        # this test stayed green for weeks while the live call failed on every
        # run — the surrogate return value hid the shape contract entirely.
        return [{"id": 1, "name": OCC_MACHINE_MINTED_LABEL}]

    monkeypatch.setattr(effect_mod, "rest_json_array", _capture)
    HandlerOccCompanionEffect()._apply_machine_minted_label(
        "OmniNode-ai", "onex_change_control", 4284, "t"
    )
    assert captured["method"] == "POST"
    # OMN-16071: the author-time POST now carries `ci:ready` alongside the
    # provenance marker. Without it a dev-targeting companion can never satisfy
    # CI Summary (the OMN-15731 label-gated pilot skips `pre-commit`, and the
    # strict block fails closed on a skip), so the writer was minting PRs that
    # only a human could unstall.
    assert captured["body"] == {"labels": [OCC_MACHINE_MINTED_LABEL, "ci:ready"]}
