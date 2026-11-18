# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests of node_ci_checkout_effect's handler."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_ci_checkout_effect.handlers.adapter_ci_checkout_git import (
    InMemoryCheckoutGit,
    RecordingCloneSync,
)
from omnimarket.nodes.node_ci_checkout_effect.handlers.handler_ci_checkout import (
    HandlerCiCheckout,
)
from omnimarket.nodes.node_ci_checkout_effect.models.model_ci_checkout import (
    EnumCiCheckoutStatus,
    ModelCiCheckoutBusProfile,
    ModelCiCheckoutOverlay,
    ModelCiCheckoutRequest,
    ModelCiCheckoutResult,
    ModelCiCheckoutSource,
)

SHA = "a" * 40
REPO = "owner/repo"
HOST = "h001"


def _overlay(
    *,
    sources: tuple[ModelCiCheckoutSource, ...] | None = None,
    sync_on_miss: bool = True,
) -> ModelCiCheckoutOverlay:
    if sources is None:
        sources = (
            ModelCiCheckoutSource(
                name="mirror",
                path_template="/mirrors/{repo_name}.git",
                serve_url_template="file:///mirrors/{repo_name}.git",
            ),
        )
    return ModelCiCheckoutOverlay(
        host_name=HOST,
        github_owner="owner",
        sources=sources,
        sync_on_miss=sync_on_miss,
        sync_command=("git", "-C", "{repo}", "fetch", "--all"),
        sync_timeout_seconds=30.0,
        git_timeout_seconds=10.0,
        reply_timeout_seconds=60.0,
        default_profile="inmemory",
        profiles={"inmemory": ModelCiCheckoutBusProfile(kind="inmemory")},
    )


def _request(**overrides) -> ModelCiCheckoutRequest:
    kwargs = {
        "correlation_id": uuid4(),
        "repo": REPO,
        "sha": SHA,
        "ref": None,
        "requested_by_lane": "lane-a",
        "host": HOST,
        **overrides,
    }
    return ModelCiCheckoutRequest(**kwargs)


def _handler(
    *,
    held: dict[str, set[str]] | None = None,
    sync_on_miss: bool = True,
    sources: tuple[ModelCiCheckoutSource, ...] | None = None,
) -> tuple[HandlerCiCheckout, RecordingCloneSync]:
    if held is None:
        held = {"/mirrors/repo.git": {SHA}}
    overlay = _overlay(sync_on_miss=sync_on_miss, sources=sources)
    git = InMemoryCheckoutGit(held)
    sync = RecordingCloneSync()
    handler = HandlerCiCheckout(overlay=overlay, git=git, sync=sync)
    return handler, sync


def test_served_from_first_source():
    handler, sync = _handler()
    result = handler.handle(_request())
    assert result.status is EnumCiCheckoutStatus.SERVED
    assert result.source_name == "mirror"
    assert result.serve_url == "file:///mirrors/repo.git"
    assert result.upstream_fetches == 0
    assert sync.calls == []


def test_second_source_when_first_lacks_it():
    sources = (
        ModelCiCheckoutSource(
            name="mirror",
            path_template="/mirrors/{repo_name}.git",
            serve_url_template="file:///mirrors/{repo_name}.git",
        ),
        ModelCiCheckoutSource(
            name="clone",
            path_template="/clones/{repo_name}.git",
            serve_url_template="file:///clones/{repo_name}.git",
        ),
    )
    held = {"/clones/repo.git": {SHA}}
    handler, _sync = _handler(held=held, sources=sources)
    result = handler.handle(_request())
    assert result.status is EnumCiCheckoutStatus.SERVED
    assert result.source_name == "clone"
    assert result.serve_url == "file:///clones/repo.git"
    assert result.upstream_fetches == 0


def test_sync_on_miss_then_served():
    held: dict[str, set[str]] = {"/mirrors/repo.git": set()}

    # The sync brings the sha: the held map changes only when the sync runs.
    def sync_brings_the_sha(_repo: str, _ref: str | None) -> None:
        held["/mirrors/repo.git"].add(SHA)

    sync = RecordingCloneSync(on_sync=sync_brings_the_sha)
    handler = HandlerCiCheckout(
        overlay=_overlay(sync_on_miss=True), git=InMemoryCheckoutGit(held), sync=sync
    )
    result = handler.handle(_request())
    assert result.status is EnumCiCheckoutStatus.SYNCED_ON_MISS
    assert result.source_name == "mirror"
    assert result.serve_url == "file:///mirrors/repo.git"
    assert result.upstream_fetches == 1
    assert sync.calls == [(REPO, None)]


def test_failing_control_a_sync_that_brings_nothing_is_not_served():
    held: dict[str, set[str]] = {"/mirrors/repo.git": set()}
    handler = HandlerCiCheckout(
        overlay=_overlay(sync_on_miss=True),
        git=InMemoryCheckoutGit(held),
        sync=RecordingCloneSync(),
    )
    assert handler.handle(_request()).status is EnumCiCheckoutStatus.NOT_FOUND


def test_not_found_after_sync():
    held: dict[str, set[str]] = {"/mirrors/repo.git": set()}
    handler, sync = _handler(held=held)
    result = handler.handle(_request())
    assert result.status is EnumCiCheckoutStatus.NOT_FOUND
    assert result.upstream_fetches == 1
    assert result.serve_url is None
    assert result.source_name is None
    detail = result.detail or ""
    # The detail names the sha and the sources that were tried.
    assert SHA in detail
    assert "mirror" in detail
    assert sync.calls == [(REPO, None)]


def test_sync_on_miss_false():
    held: dict[str, set[str]] = {"/mirrors/repo.git": set()}
    handler, sync = _handler(held=held, sync_on_miss=False)
    result = handler.handle(_request())
    assert result.status is EnumCiCheckoutStatus.NOT_FOUND
    assert result.upstream_fetches == 0
    assert sync.calls == []


def test_sync_fails():
    held: dict[str, set[str]] = {"/mirrors/repo.git": set()}
    overlay = _overlay(sync_on_miss=True)
    git = InMemoryCheckoutGit(held)

    class FailingSync:
        def sync(self, repo: str, ref: str | None) -> bool:
            return False

    handler = HandlerCiCheckout(overlay=overlay, git=git, sync=FailingSync())
    result = handler.handle(_request())
    assert result.status is EnumCiCheckoutStatus.FAILED
    assert result.upstream_fetches == 1
    assert result.detail == "clone sync failed"


def test_exception_becomes_failed_with_type_name():
    overlay = _overlay()

    class RaisingGit:
        def has_commit(self, repo_path: str, sha: str) -> bool:
            raise RuntimeError("boom")

    handler = HandlerCiCheckout(
        overlay=overlay, git=RaisingGit(), sync=RecordingCloneSync()
    )
    result = handler.handle(_request())
    assert result.status is EnumCiCheckoutStatus.FAILED
    assert result.detail == "RuntimeError"


def test_refused_when_host_mismatches():
    handler, _ = _handler()
    result = handler.handle(_request(host="other-host"))
    assert result.status is EnumCiCheckoutStatus.REFUSED
    assert result.detail == "addressed to another host"
    assert result.serve_url is None


def test_model_validators_served_requires_serve_url():
    with pytest.raises(ValidationError):
        ModelCiCheckoutResult(
            correlation_id=uuid4(),
            host=HOST,
            status=EnumCiCheckoutStatus.SERVED,
            repo=REPO,
            sha=SHA,
            source_name="mirror",
            serve_url=None,
        )


def test_model_validators_not_found_requires_no_serve_url():
    with pytest.raises(ValidationError):
        ModelCiCheckoutResult(
            correlation_id=uuid4(),
            host=HOST,
            status=EnumCiCheckoutStatus.NOT_FOUND,
            repo=REPO,
            sha=SHA,
            serve_url="file:///x",
        )


def test_overlay_missing_key_refuses_naming_it():
    with pytest.raises(ValidationError) as exc_info:
        ModelCiCheckoutOverlay(
            host_name=HOST,
            github_owner="owner",
            sources=(
                ModelCiCheckoutSource(
                    name="mirror",
                    path_template="/m/{repo_name}.git",
                    serve_url_template="file:///m/{repo_name}.git",
                ),
            ),
            sync_on_miss=True,
            sync_command=("git",),
            sync_timeout_seconds=30.0,
            git_timeout_seconds=10.0,
            reply_timeout_seconds=60.0,
            default_profile="inmemory",
            # profiles missing
        )
    assert "profiles" in str(exc_info.value)


def test_overlay_syncing_on_a_miss_requires_a_sync_command():
    """An empty sync_command is refused exactly when the overlay syncs on a miss."""
    base: dict[str, object] = {
        "host_name": HOST,
        "github_owner": "owner",
        "sources": (
            ModelCiCheckoutSource(
                name="mirror",
                path_template="/m/{repo_name}.git",
                serve_url_template="file:///m/{repo_name}.git",
            ),
        ),
        "sync_timeout_seconds": 30.0,
        "git_timeout_seconds": 10.0,
        "reply_timeout_seconds": 60.0,
        "default_profile": "inmemory",
        "profiles": {"inmemory": ModelCiCheckoutBusProfile(kind="inmemory")},
    }
    with pytest.raises(ValidationError) as exc_info:
        ModelCiCheckoutOverlay(**base, sync_on_miss=True, sync_command=())
    assert "sync_command" in str(exc_info.value)
    # The same empty command is fine when a miss is not synced.
    overlay = ModelCiCheckoutOverlay(**base, sync_on_miss=False, sync_command=())
    assert overlay.sync_command == ()


def test_failing_control_reversed_source_order_changes_result():
    """Proves the handler reads the source order: reversed, the answer changes."""
    sources_a = (
        ModelCiCheckoutSource(
            name="first",
            path_template="/a/{repo_name}.git",
            serve_url_template="file:///a/{repo_name}.git",
        ),
        ModelCiCheckoutSource(
            name="second",
            path_template="/b/{repo_name}.git",
            serve_url_template="file:///b/{repo_name}.git",
        ),
    )
    sources_b = (sources_a[1], sources_a[0])
    held = {"/a/repo.git": {SHA}, "/b/repo.git": {SHA}}

    handler_a, _ = _handler(held=held, sources=sources_a)
    result_a = handler_a.handle(_request())
    assert result_a.source_name == "first"

    handler_b, _ = _handler(held=held, sources=sources_b)
    result_b = handler_b.handle(_request())
    assert result_b.source_name == "second"
    assert result_a.source_name != result_b.source_name
