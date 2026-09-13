# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18334: the write-EFFECT repairs a dropped evidence line with one write.

The compute half of this ticket decides that a repair is owed
(``plan.reassert_stamp``). This module pins the write half: exactly one product
body patch, no clone, no branch, no companion PR, and re-entrant -- a second run
over a repaired body reaches the already-bound no-op and patches nothing.

The clone/push path is never reached on a repair, so these tests run offline
against a stub read-EFFECT and a recording body-patcher. A repair that reached
the git path would be a second companion, which is the defect this ticket
removes.
"""

from __future__ import annotations

import pytest

from omnimarket.events.occ_companion import ModelOccExistingCompanion
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
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

pytestmark = pytest.mark.unit

_REPO = "OmniNode-ai/omnimarket"
_PR = 1760
_OCC_PR = 4242
_STAMP = f"Evidence-Source: OCC#{_OCC_PR}"

#: The description after a lane rewrote it: prose kept, evidence block gone.
_LOSSY_BODY = "Closes OMN-14608\n\nRewritten by hand.\n"


class _StubStateHandler(HandlerOccStateEffect):
    """RSD-2 read stub — returns a canned request, performs no I/O."""

    def __init__(self, request: ModelOccCompanionRequest) -> None:
        self._request = request

    async def handle(self, request: ModelOccStateRequest) -> ModelOccCompanionRequest:
        return self._request


def _canned(**overrides: object) -> ModelOccCompanionRequest:
    base = ModelOccCompanionRequest(
        repo=_REPO,
        pr_number=_PR,
        pr_head_sha="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
        pr_title="feat(OMN-14608): thing",
        pr_body=_LOSSY_BODY,
        run_timestamp="2026-09-13T19:00:00Z",
        product_probe=ModelObservedProbe(
            command=f"gh pr view {_PR}",
            stdout=f'{{"number":{_PR},"state":"OPEN"}}',
            exit_code=0,
        ),
        existing_companion=ModelOccExistingCompanion(
            pr_number=_OCC_PR,
            state="open",
            merged=False,
            head_branch=f"auto/{_REPO.replace('/', '-').lower()}-pr-{_PR}-occ-autobind",
        ),
    )
    return base.model_copy(update=dict(overrides)) if overrides else base


class _Recorder:
    """Records every product-body patch the handler attempts."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def patch(
        self,
        product_owner: str,
        product_name: str,
        pr_number: int,
        new_body: str,
        current_body: str,
        token: str,
        *,
        product_token_dedicated: bool = False,
    ) -> bool:
        if not new_body or new_body == current_body:
            return False
        self.calls.append((f"{product_owner}/{product_name}#{pr_number}", new_body))
        return True


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(
        HandlerOccCompanionEffect, "_patch_product_body", rec.patch, raising=True
    )
    monkeypatch.setenv("GITHUB_TOKEN", "fixture-token-not-a-credential")
    return rec


def _stamp_lines(body: str) -> list[str]:
    return [
        line
        for line in body.splitlines()
        if line.strip().lower().startswith("evidence-source:")
    ]


async def _run(request: ModelOccCompanionRequest, mode: str = "mutate"):
    handler = HandlerOccCompanionEffect(state_handler=_StubStateHandler(request))
    return await handler.handle(
        ModelOccCompanionEffectRequest(repo=_REPO, pr_number=_PR, mode=mode)
    )


# --------------------------------------------------------------------------
# AC2 -- the dropped line is re-appended
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_dropped_line_is_re_asserted_with_one_write(
    recorder: _Recorder,
) -> None:
    """RED before this ticket: the effect took the git path and minted again."""
    result = await _run(_canned())

    assert result.product_body_stamped is True
    assert result.occ_pr_number == _OCC_PR
    assert result.companion_paths == ()
    assert len(recorder.calls) == 1, recorder.calls
    target, new_body = recorder.calls[0]
    assert target == f"{_REPO}#{_PR}"
    assert _stamp_lines(new_body) == [_STAMP]
    assert "Rewritten by hand." in new_body


@pytest.mark.asyncio
async def test_the_receipt_line_says_it_repaired_rather_than_authored(
    recorder: _Recorder,
) -> None:
    result = await _run(_canned())
    assert "re-assert" in result.action.lower()
    assert str(_OCC_PR) in result.action


@pytest.mark.asyncio
async def test_a_merged_product_pr_is_repaired_too(recorder: _Recorder) -> None:
    result = await _run(_canned(pr_state="closed", pr_merged=True))
    assert result.product_body_stamped is True
    assert len(recorder.calls) == 1


# --------------------------------------------------------------------------
# AC3 -- re-entrant, exactly one line
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_twice_writes_once_and_leaves_one_line(
    recorder: _Recorder,
) -> None:
    first = await _run(_canned())
    assert len(recorder.calls) == 1
    repaired = recorder.calls[0][1]

    second = await _run(_canned(pr_body=repaired))
    assert second.no_op is True
    assert len(recorder.calls) == 1, "the second run must not patch the body again"
    assert _stamp_lines(repaired) == [_STAMP]
    assert first.product_body_stamped is True


@pytest.mark.asyncio
async def test_no_companion_means_no_repair(recorder: _Recorder) -> None:
    """Positive control: without an existing companion the born path is unchanged.

    ``dry_run`` so the born path stops before any git side effect; what is
    asserted is that the plan is an AUTHORING plan, not a repair.
    """
    result = await _run(_canned(existing_companion=None), mode="dry_run")
    assert recorder.calls == []
    assert result.no_op is False
    assert any(p.startswith("contracts/") for p in result.companion_paths)


@pytest.mark.asyncio
async def test_dry_run_never_patches(recorder: _Recorder) -> None:
    result = await _run(_canned(), mode="dry_run")
    assert recorder.calls == []
    assert result.product_body_stamped is False
    assert "dry_run" in result.action
