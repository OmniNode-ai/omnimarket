# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for HandlerGeneratedNodePublishEffect (OMN-13606, SEA Phase 0.2).

Covers the deterministic publish flow and key error branches. All git/gh
subprocess I/O is exercised via the injected ``run_fn`` callable -- no real
git/gh calls. The terminal emit is captured via an injected ``event_publisher``
so the contract-declared publish topic is asserted without a live bus.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_generated_node_publish_effect.handlers.handler_generated_node_publish_effect import (
    HandlerGeneratedNodePublishEffect,
)
from omnimarket.nodes.node_generated_node_publish_effect.models.model_generated_node_publish_input import (
    ModelGeneratedNodePublishInput,
)
from omnimarket.nodes.node_generated_node_publish_effect.models.model_generated_node_publish_result import (
    ModelGeneratedNodePublishResult,
)

REPO = "OmniNode-ai/omnimarket"
NODE_NAME = "node_demo_widget_compute"
TICKET = "OMN-13606"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run(
    responses: list[tuple[int, str, str]],
) -> tuple[Callable[[list[str]], tuple[int, str, str]], list[list[str]]]:
    """Return a (run_fn, captured_commands) pair. run_fn pops responses in order."""
    captured: list[list[str]] = []
    idx = 0

    def _run(cmd: list[str]) -> tuple[int, str, str]:
        nonlocal idx
        captured.append(list(cmd))
        rc, out, err = responses[idx]
        idx += 1
        return rc, out, err

    return _run, captured


def _make_capture_publisher() -> tuple[
    Callable[[str, bytes], None], list[tuple[str, bytes]]
]:
    captured: list[tuple[str, bytes]] = []

    def _publish(topic: str, payload: bytes) -> None:
        captured.append((topic, payload))

    return _publish, captured


def _staged_package(tmp_path: Path) -> Path:
    """Create a minimal staged canonical package on disk."""
    pkg = tmp_path / "staging" / NODE_NAME
    pkg.mkdir(parents=True)
    (pkg / "contract.yaml").write_text("name: " + NODE_NAME + "\n", encoding="utf-8")
    (pkg / "__init__.py").write_text('"""pkg."""\n', encoding="utf-8")
    return pkg


def _input(staging_dir: Path, **overrides: object) -> ModelGeneratedNodePublishInput:
    base: dict[str, object] = {
        "correlation_id": uuid4(),
        "node_name": NODE_NAME,
        "staging_dir": str(staging_dir),
        "repo": REPO,
        "ticket": TICKET,
        "dod_evidence": (
            "golden-chain proof: generated node scaffolded + invoked in-session"
        ),
    }
    base.update(overrides)
    return ModelGeneratedNodePublishInput(**base)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_happy_path_opens_pr_and_emits_url(tmp_path: Path) -> None:
    """A staged package is committed to a worktree branch and a PR is opened."""
    staging = _staged_package(tmp_path)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    pr_url = "https://github.com/OmniNode-ai/omnimarket/pull/9999"

    # git worktree add ; git add ; git commit ; git push ; gh pr create
    run_fn, captured = _make_run(
        [
            (0, "", ""),  # git worktree add
            (0, "", ""),  # git add
            (0, "", ""),  # git commit
            (0, "", ""),  # git push
            (0, pr_url + "\n", ""),  # gh pr create -> prints URL
        ]
    )
    publish_fn, published = _make_capture_publisher()

    handler = HandlerGeneratedNodePublishEffect(
        run_fn=run_fn,
        repo_root_resolver=lambda _repo: repo_root,
        event_publisher=publish_fn,
    )

    result = await handler.handle(_input(staging))

    assert isinstance(result, ModelGeneratedNodePublishResult)
    assert result.published is True
    assert result.pr_url == pr_url
    assert result.blocked_reason is None
    assert result.node_name == NODE_NAME

    # gh pr create was invoked with the repo and a body carrying ticket + dod_evidence.
    gh_cmd = next(c for c in captured if c[:3] == ["gh", "pr", "create"])
    assert "--repo" in gh_cmd
    assert REPO in gh_cmd
    body = gh_cmd[gh_cmd.index("--body") + 1]
    assert TICKET in body
    assert "dod_evidence" in body.lower() or "golden-chain" in body.lower()

    # PR title carries the ticket reference.
    title = gh_cmd[gh_cmd.index("--title") + 1]
    assert TICKET in title

    # Terminal emit lands on the contract-declared publish topic with the URL.
    assert len(published) == 1
    topic, payload = published[0]
    assert topic == handler.terminal_topic
    emitted = json.loads(payload.decode("utf-8"))
    assert emitted["pr_url"] == pr_url
    assert emitted["published"] is True


@pytest.mark.unit
async def test_missing_staging_dir_blocks(tmp_path: Path) -> None:
    """A non-existent staging dir is a hard block -- no git/gh calls made."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    run_fn, captured = _make_run([])
    handler = HandlerGeneratedNodePublishEffect(
        run_fn=run_fn,
        repo_root_resolver=lambda _repo: repo_root,
    )

    missing = tmp_path / "nope" / NODE_NAME
    result = await handler.handle(_input(missing))

    assert result.published is False
    assert result.pr_url is None
    assert "staging" in (result.blocked_reason or "").lower()
    assert captured == []


@pytest.mark.unit
async def test_git_push_failure_blocks(tmp_path: Path) -> None:
    """A non-zero push exit blocks publish before gh pr create is attempted."""
    staging = _staged_package(tmp_path)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    run_fn, captured = _make_run(
        [
            (0, "", ""),  # git worktree add
            (0, "", ""),  # git add
            (0, "", ""),  # git commit
            (128, "", "remote rejected"),  # git push fails
        ]
    )
    handler = HandlerGeneratedNodePublishEffect(
        run_fn=run_fn,
        repo_root_resolver=lambda _repo: repo_root,
    )

    result = await handler.handle(_input(staging))

    assert result.published is False
    assert result.pr_url is None
    assert "push" in (result.blocked_reason or "").lower()
    # gh pr create must NOT have been attempted after a failed push.
    assert not any(c[:3] == ["gh", "pr", "create"] for c in captured)


@pytest.mark.unit
async def test_gh_pr_create_failure_blocks(tmp_path: Path) -> None:
    """A failed gh pr create is surfaced as a block with no PR URL."""
    staging = _staged_package(tmp_path)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    run_fn, _captured = _make_run(
        [
            (0, "", ""),  # git worktree add
            (0, "", ""),  # git add
            (0, "", ""),  # git commit
            (0, "", ""),  # git push
            (1, "", "gh: could not create pull request"),  # gh pr create fails
        ]
    )
    handler = HandlerGeneratedNodePublishEffect(
        run_fn=run_fn,
        repo_root_resolver=lambda _repo: repo_root,
    )

    result = await handler.handle(_input(staging))

    assert result.published is False
    assert result.pr_url is None
    assert "pr create" in (result.blocked_reason or "").lower()
