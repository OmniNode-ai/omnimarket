# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for HandlerGeneratedNodePublishEffect (OMN-13606 / OMN-13625, SEA Phase 0.2 + 7.2).

Covers the deterministic publish flow and key error branches. All git/gh
subprocess I/O is exercised via the injected ``run_fn`` callable -- no real
git/gh calls. The terminal emit is captured via an injected ``event_publisher``
so the contract-declared publish topic is asserted without a live bus.

Phase 7.2 tests (OMN-13625) cover ``_derive_module_prefix`` and
``_patch_pyproject_entry_point`` as pure functions, plus the
``HandlerGeneratedNodePublishEffect._register_entry_point`` method which
delegates to those helpers. Existing happy-path tests use
``register_entry_point=False`` since the fake ``run_fn`` does not actually
create a worktree directory that would contain a real pyproject.toml; the
dedicated Phase 7.2 tests exercise registration against a real tmp_path tree.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_generated_node_publish_effect.handlers.handler_generated_node_publish_effect import (
    HandlerGeneratedNodePublishEffect,
    _derive_module_prefix,
    _patch_pyproject_entry_point,
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
    """A staged package is committed to a worktree branch and a PR is opened.

    ``register_entry_point=False`` here because the fake ``run_fn`` does not
    physically create the worktree directory; the real pyproject.toml tests are
    in ``TestEntryPointRegistration`` below.
    """
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

    result = await handler.handle(_input(staging, register_entry_point=False))

    assert isinstance(result, ModelGeneratedNodePublishResult)
    assert result.published is True
    assert result.pr_url == pr_url
    assert result.blocked_reason is None
    assert result.node_name == NODE_NAME
    # registration was explicitly disabled in this test
    assert result.entry_point_registered is False

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
    result = await handler.handle(_input(missing, register_entry_point=False))

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

    result = await handler.handle(_input(staging, register_entry_point=False))

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

    result = await handler.handle(_input(staging, register_entry_point=False))

    assert result.published is False
    assert result.pr_url is None
    assert "pr create" in (result.blocked_reason or "").lower()


# ---------------------------------------------------------------------------
# Phase 7.2 (OMN-13625) — entry-point auto-registration
# ---------------------------------------------------------------------------

_MINIMAL_PYPROJECT = """\
[project]
name = "omnimarket"
version = "0.1.0"

[project.entry-points."onex.nodes"]
node_existing_compute = "omnimarket.nodes.node_existing_compute"
node_another_effect = "omnimarket.nodes.node_another_effect"

[project.entry-points."onex.cli"]
market = "omnimarket.cli.market:market"
"""


@pytest.mark.unit
class TestDeriveModulePrefix:
    """_derive_module_prefix correctly strips the src layout prefix."""

    def test_src_layout_stripped(self) -> None:
        assert _derive_module_prefix("src/omnimarket/nodes") == "omnimarket.nodes"

    def test_no_src_prefix_kept_as_is(self) -> None:
        assert _derive_module_prefix("omnimarket/nodes") == "omnimarket.nodes"

    def test_deep_path(self) -> None:
        assert _derive_module_prefix("src/foo/bar/baz") == "foo.bar.baz"

    def test_single_segment_no_src(self) -> None:
        assert _derive_module_prefix("omnimarket") == "omnimarket"


@pytest.mark.unit
class TestPatchPyprojectEntryPoint:
    """_patch_pyproject_entry_point patches pyproject.toml correctly."""

    def _write(self, tmp_path: Path, content: str) -> Path:
        p = tmp_path / "pyproject.toml"
        p.write_text(content, encoding="utf-8")
        return p

    def test_adds_new_entry_in_onex_nodes_section(self, tmp_path: Path) -> None:
        p = self._write(tmp_path, _MINIMAL_PYPROJECT)
        registered, blocked = _patch_pyproject_entry_point(
            p, "node_new_widget_compute", "omnimarket.nodes"
        )
        assert blocked is None
        assert registered is True
        text = p.read_text(encoding="utf-8")
        assert (
            'node_new_widget_compute = "omnimarket.nodes.node_new_widget_compute"'
            in text
        )

    def test_idempotent_second_call_does_not_duplicate(self, tmp_path: Path) -> None:
        p = self._write(tmp_path, _MINIMAL_PYPROJECT)
        _patch_pyproject_entry_point(p, "node_new_widget_compute", "omnimarket.nodes")
        _patch_pyproject_entry_point(p, "node_new_widget_compute", "omnimarket.nodes")
        text = p.read_text(encoding="utf-8")
        # Count the full entry-point LINE (not the bare node_name substring, which
        # also appears inside the value string and would give count==4 if duplicated).
        entry_line = (
            'node_new_widget_compute = "omnimarket.nodes.node_new_widget_compute"'
        )
        count = text.count(entry_line)
        assert count == 1, f"entry line duplicated; found {count} occurrences"

    def test_existing_entry_is_idempotent(self, tmp_path: Path) -> None:
        p = self._write(tmp_path, _MINIMAL_PYPROJECT)
        registered, blocked = _patch_pyproject_entry_point(
            p, "node_existing_compute", "omnimarket.nodes"
        )
        assert blocked is None
        assert registered is True
        # Count the full entry-point LINE; it should appear exactly once (unchanged).
        entry_line = 'node_existing_compute = "omnimarket.nodes.node_existing_compute"'
        assert p.read_text(encoding="utf-8").count(entry_line) == 1

    def test_missing_section_returns_blocked_reason(self, tmp_path: Path) -> None:
        p = self._write(tmp_path, '[project]\nname = "omnimarket"\n')
        registered, blocked = _patch_pyproject_entry_point(
            p, "node_foo_compute", "omnimarket.nodes"
        )
        assert registered is False
        assert blocked is not None
        assert "onex.nodes" in blocked

    def test_missing_pyproject_returns_blocked_reason(self, tmp_path: Path) -> None:
        missing = tmp_path / "pyproject.toml"
        registered, blocked = _patch_pyproject_entry_point(
            missing, "node_foo_compute", "omnimarket.nodes"
        )
        assert registered is False
        assert blocked is not None
        assert "not found" in blocked

    def test_new_entry_appears_before_next_section(self, tmp_path: Path) -> None:
        """Entry is inserted inside onex.nodes, NOT after [onex.cli]."""
        p = self._write(tmp_path, _MINIMAL_PYPROJECT)
        _patch_pyproject_entry_point(p, "node_inserted_compute", "omnimarket.nodes")
        text = p.read_text(encoding="utf-8")
        nodes_idx = text.index('[project.entry-points."onex.nodes"]')
        cli_idx = text.index('[project.entry-points."onex.cli"]')
        inserted_idx = text.index("node_inserted_compute")
        assert nodes_idx < inserted_idx < cli_idx


@pytest.mark.unit
class TestHandlerRegisterEntryPointMethod:
    """HandlerGeneratedNodePublishEffect._register_entry_point integration."""

    def _make_handler(self) -> HandlerGeneratedNodePublishEffect:
        return HandlerGeneratedNodePublishEffect(
            run_fn=lambda _cmd: (0, "", ""),
            repo_root_resolver=lambda _repo: Path("/tmp/fake-repo"),
        )

    def test_patches_pyproject_in_worktree(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(_MINIMAL_PYPROJECT, encoding="utf-8")

        handler = self._make_handler()
        registered, blocked = handler._register_entry_point(
            tmp_path, "node_demo_widget_compute", "src/omnimarket/nodes"
        )
        assert blocked is None
        assert registered is True
        text = pyproject.read_text(encoding="utf-8")
        assert (
            'node_demo_widget_compute = "omnimarket.nodes.node_demo_widget_compute"'
            in text
        )

    def test_missing_pyproject_returns_blocked(self, tmp_path: Path) -> None:
        handler = self._make_handler()
        registered, blocked = handler._register_entry_point(
            tmp_path, "node_demo_widget_compute", "src/omnimarket/nodes"
        )
        assert registered is False
        assert blocked is not None

    async def test_register_entry_point_false_skips_registration(
        self, tmp_path: Path
    ) -> None:
        """When register_entry_point=False the result reports entry_point_registered=False."""
        # No pyproject.toml in tmp_path -- would block if registration were attempted.
        staging = tmp_path / "staging" / NODE_NAME
        staging.mkdir(parents=True)
        (staging / "contract.yaml").write_text("name: " + NODE_NAME + "\n")
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        pr_url = "https://github.com/OmniNode-ai/omnimarket/pull/1234"

        run_fn, _captured = _make_run(
            [
                (0, "", ""),  # git worktree add
                (0, "", ""),  # git add
                (0, "", ""),  # git commit
                (0, "", ""),  # git push
                (0, pr_url + "\n", ""),  # gh pr create
            ]
        )
        handler = HandlerGeneratedNodePublishEffect(
            run_fn=run_fn,
            repo_root_resolver=lambda _repo: repo_root,
        )
        result = await handler.handle(_input(staging, register_entry_point=False))
        assert result.published is True
        assert result.entry_point_registered is False
