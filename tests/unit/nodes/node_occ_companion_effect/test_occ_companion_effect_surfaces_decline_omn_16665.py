# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16665: the write-EFFECT must SURFACE a decline and FAIL on a lost one.

The compute half decides *why* a companion was declined; this module pins what
the write-EFFECT does with that decision. Two properties, matching the two
failure modes the ticket was filed for:

* **Surfaced** — an idempotent comment lands on the product PR quoting the
  matched text (AC1/AC2), and a re-trigger with the identical decline does NOT
  re-post (AC4). Pre-16665 the reason reached a ``.201`` log line and nothing
  else, so from the PR author's seat a correct policy decline and a dead
  consumer were the same observation.

* **Loud** — an ``evidence_lost`` decline RAISES rather than returning a
  ``no_op`` result. The raise is the whole point: the runtime routes a returned
  result to ``occ-companion-effect-completed.v1`` (success) and a raised error to
  ``occ-companion-effect-failed.v1``. Reporting a permanently-missing evidence
  record on the success topic is what made omnimemory#447's hole invisible to
  every projection. ``feedback_gates_block_no_bypass``: no warn-only.

GitHub is stubbed at the ``rest_json``/``rest_json_array`` seam the handler
actually calls, so these assert the real code path rather than a re-implemented
one.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
)
from omnimarket.nodes.node_occ_companion_effect.handlers import (
    handler_occ_companion_effect as effect_module,
)
from omnimarket.nodes.node_occ_companion_effect.handlers.handler_occ_companion_effect import (
    HandlerOccCompanionEffect,
    OccCompanionEvidenceLostError,
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

_REPO = "OmniNode-ai/omnimemory"
_PR = 447


class _StubStateHandler(HandlerOccStateEffect):
    """RSD-2 read stub — returns a canned request, performs no I/O."""

    def __init__(self, request: ModelOccCompanionRequest) -> None:
        self._request = request
        self.seen: list[ModelOccStateRequest] = []

    async def handle(self, request: ModelOccStateRequest) -> ModelOccCompanionRequest:
        self.seen.append(request)
        return self._request


class _FakeGitHub:
    """Records POSTs and replays the comments the test says already exist."""

    def __init__(self, existing_comments: list[dict[str, Any]] | None = None) -> None:
        self.existing: list[dict[str, Any]] = list(existing_comments or [])
        self.comments_posted: list[dict[str, Any]] = []
        self.check_runs_posted: list[dict[str, Any]] = []

    def rest_json(
        self, method: str, path: str, *, token: str, body: Any = None, **_: Any
    ) -> dict[str, Any]:
        if method == "POST" and path.endswith("/comments"):
            self.comments_posted.append(body)
            # Mirror GitHub: a posted comment is visible to the next read. This
            # is what makes the idempotency assertion a real one rather than a
            # test of an empty list.
            self.existing.append({"body": body["body"]})
            return {"id": 1}
        if method == "POST" and path.endswith("/check-runs"):
            self.check_runs_posted.append(body)
            return {"id": 2}
        raise AssertionError(f"unexpected {method} {path}")

    def rest_json_array(
        self, method: str, path: str, *, token: str, **_: Any
    ) -> list[dict[str, Any]]:
        if method == "GET" and "/comments" in path:
            return list(self.existing)
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> _FakeGitHub:
    fake = _FakeGitHub()
    monkeypatch.setattr(effect_module, "rest_json", fake.rest_json)
    monkeypatch.setattr(effect_module, "rest_json_array", fake.rest_json_array)
    monkeypatch.setattr(effect_module, "_resolve_github_token", lambda: "occ-token")
    monkeypatch.setattr(
        effect_module, "_resolve_product_token", lambda _t: ("product-token", True)
    )
    return fake


def _canned(**overrides: object) -> ModelOccCompanionRequest:
    base: dict[str, object] = {
        "repo": _REPO,
        "pr_number": _PR,
        "pr_head_sha": "a" * 40,
        "pr_title": "docs(OMN-16669): correct the settings docstring default",
        "pr_body": "Fixes the drift. See OMN-16669.",
        "pr_state": "open",
        "changed_files": ("src/omnimemory/settings.py", "README.md"),
        "diff_total_lines": 9,
        "run_timestamp": "2026-08-26T19:47:00Z",
        "product_probe": ModelObservedProbe(
            command=f"gh pr view {_PR}",
            stdout=f'{{"number":{_PR},"state":"MERGED"}}',
            exit_code=0,
        ),
    }
    base.update(overrides)
    return ModelOccCompanionRequest(**base)  # type: ignore[arg-type]


def _handler(request: ModelOccCompanionRequest) -> HandlerOccCompanionEffect:
    return HandlerOccCompanionEffect(state_handler=_StubStateHandler(request))


def _command(**overrides: object) -> ModelOccCompanionEffectRequest:
    base: dict[str, object] = {"repo": _REPO, "pr_number": _PR, "mode": "mutate"}
    base.update(overrides)
    return ModelOccCompanionEffectRequest(**base)  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.asyncio
class TestRecoverableDeclineIsSurfaced:
    async def test_hold_marker_decline_posts_a_comment_quoting_the_match(
        self, github: _FakeGitHub
    ) -> None:
        held = _canned(
            pr_body=(
                "Fixes OMN-16669.\n"
                "\n"
                "## Test plan\n"
                "CI may not run; do not merge until CI is confirmed green.\n"
            )
        )
        result = await _handler(held).handle(_command())

        assert result.no_op is True
        assert result.suppression_surfaced is True
        assert len(github.comments_posted) == 1
        body = github.comments_posted[0]["body"]
        # AC2: the actual matched substring, not just the rule name.
        assert "`do not merge`" in body
        assert "line 4" in body
        # The remediation must be present, or the comment is a shrug.
        assert "workflow_dispatch" in body

    async def test_recoverable_decline_check_run_is_neutral_not_failure(
        self, github: _FakeGitHub
    ) -> None:
        """Observability must never newly block a PR that policy only paused."""
        await _handler(_canned(pr_is_draft=True)).handle(_command())

        assert len(github.check_runs_posted) == 1
        assert github.check_runs_posted[0]["conclusion"] == "neutral"
        assert github.check_runs_posted[0]["head_sha"] == "a" * 40

    async def test_identical_decline_is_not_reposted_on_retrigger(
        self, github: _FakeGitHub
    ) -> None:
        """AC1/AC4 idempotency: a ``synchronize`` storm must not spam the PR."""
        held = _canned(pr_labels=("do-not-merge",))
        handler = _handler(held)

        first = await handler.handle(_command())
        second = await handler.handle(_command())

        assert first.suppression_surfaced is True
        assert second.suppression_surfaced is False
        assert len(github.comments_posted) == 1

    async def test_a_changed_decline_does_post_again(self, github: _FakeGitHub) -> None:
        """Idempotency is keyed on the decline CODE, not the PR. A PR that was
        held and has since merged unbound has escalated from 'paused' to
        'evidence gone' — suppressing that second notice would re-hide exactly
        what this ticket surfaced."""
        await _handler(_canned(pr_labels=("do-not-merge",))).handle(_command())
        with pytest.raises(OccCompanionEvidenceLostError):
            await _handler(_canned(pr_state="closed", pr_merged=True)).handle(
                _command()
            )

        assert len(github.comments_posted) == 2
        assert "evidence_lost_pr_merged" in github.comments_posted[1]["body"]

    async def test_dry_run_surfaces_nothing(self, github: _FakeGitHub) -> None:
        """dry_run is read+compute only; it must not write to the product PR."""
        result = await _handler(_canned(pr_is_draft=True)).handle(
            _command(mode="dry_run")
        )

        assert result.no_op is True
        assert result.suppression_surfaced is False
        assert github.comments_posted == []
        assert github.check_runs_posted == []


@pytest.mark.unit
@pytest.mark.asyncio
class TestEvidenceLostIsLoud:
    async def test_merged_unbound_pr_raises_instead_of_returning_no_op(
        self, github: _FakeGitHub
    ) -> None:
        """The root-cause fix. A returned result goes to the -completed.v1
        SUCCESS topic; only a raise reaches -failed.v1."""
        with pytest.raises(OccCompanionEvidenceLostError) as excinfo:
            await _handler(_canned(pr_state="closed", pr_merged=True)).handle(
                _command()
            )

        message = str(excinfo.value)
        assert f"{_REPO}#{_PR}" in message
        assert "OMN-16669" in message
        assert "allow_merged_replay" in message

    async def test_evidence_lost_check_run_is_a_failure(
        self, github: _FakeGitHub
    ) -> None:
        with pytest.raises(OccCompanionEvidenceLostError):
            await _handler(_canned(pr_state="closed", pr_merged=True)).handle(
                _command()
            )

        assert github.check_runs_posted[0]["conclusion"] == "failure"

    async def test_the_decline_is_surfaced_before_the_raise(
        self, github: _FakeGitHub
    ) -> None:
        """Order matters: if the raise came first the merged PR would carry no
        record of WHY, and the failed event alone is not a surface a PR author
        ever looks at."""
        with pytest.raises(OccCompanionEvidenceLostError):
            await _handler(_canned(pr_state="closed", pr_merged=True)).handle(
                _command()
            )

        assert len(github.comments_posted) == 1
        assert "no longer mint one" in github.comments_posted[0]["body"]

    async def test_closed_unmerged_pr_does_not_raise(self, github: _FakeGitHub) -> None:
        """The discrimination, asserted at the EFFECT boundary: an abandoned PR
        must stay a quiet, successful no-op."""
        result = await _handler(_canned(pr_state="closed", pr_merged=False)).handle(
            _command()
        )

        assert result.no_op is True
        assert result.suppression_code == "pr_closed_unmerged"
        assert github.check_runs_posted[0]["conclusion"] == "neutral"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_allow_merged_replay_is_threaded_to_the_read_effect() -> None:
    """The override is useless if the command carries it and the read drops it —
    the live-state read is where it must land to reach compute."""
    stub = _StubStateHandler(_canned())
    handler = HandlerOccCompanionEffect(state_handler=stub)

    await handler.handle(_command(mode="dry_run", allow_merged_replay=True))

    assert stub.seen[0].allow_merged_replay is True
