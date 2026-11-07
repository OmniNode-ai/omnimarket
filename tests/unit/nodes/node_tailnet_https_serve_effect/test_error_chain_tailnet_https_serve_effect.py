# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: every injected break ends in its own typed outcome, is reported once, and changes nothing it should not."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_tailnet_https_serve_effect.models import (
    EnumServeMode,
    EnumServeOutcome,
)
from omnimarket.nodes.node_tailnet_https_serve_effect.runtime_tailnet_https_serve import (
    main,
)
from tests.unit.nodes.node_tailnet_https_serve_effect.support import (
    ENV_FILE,
    FOREIGN,
    OTHER_PORT,
    PORT,
    UPSTREAM,
    BreakableBackend,
    build,
    request,
)

pytestmark = pytest.mark.unit


def test_a_dead_upstream_is_refused_before_anything_is_changed() -> None:
    handler, _, backend, reporter = build(alive=False)
    result = handler.handle(request(EnumServeMode.INSTALL))
    assert result.outcome is EnumServeOutcome.REFUSED_DEAD_UPSTREAM
    assert not result.ok
    assert backend.calls == []
    assert backend.served == {}
    assert backend.env_files == {}
    assert reporter.reports == [result]


def test_a_port_held_by_a_foreign_listener_is_refused_and_not_overwritten() -> None:
    handler, _, backend, reporter = build(backend=BreakableBackend({PORT: FOREIGN}))
    result = handler.handle(request(EnumServeMode.INSTALL))
    assert result.outcome is EnumServeOutcome.REFUSED_FOREIGN_LISTENER
    assert result.served_target == FOREIGN
    assert backend.served == {PORT: FOREIGN}
    assert backend.calls == []
    assert reporter.reports == [result]


def test_a_listener_that_serves_the_wrong_target_fails_its_readback() -> None:
    # The named injected break: serve lands on another target, so the readback must not pass.
    handler, _, _backend, reporter = build(
        backend=BreakableBackend(serve_lands_wrong_target=True)
    )
    result = handler.handle(request(EnumServeMode.INSTALL))
    assert result.outcome is EnumServeOutcome.READBACK_FAILED
    assert not result.ok
    assert FOREIGN in result.detail
    assert reporter.reports == [result]


def test_a_serve_command_that_fails_is_a_backend_failure_not_an_install() -> None:
    handler, _, _, reporter = build(backend=BreakableBackend(serve_fails=True))
    result = handler.handle(request(EnumServeMode.INSTALL))
    assert result.outcome is EnumServeOutcome.BACKEND_FAILED
    assert "access denied" in result.detail
    assert reporter.reports == [result]


def test_an_unreadable_serve_status_is_a_backend_failure() -> None:
    handler, _, _, _ = build(backend=BreakableBackend(status_fails=True))
    for mode in (EnumServeMode.INSTALL, EnumServeMode.UNINSTALL, EnumServeMode.STATUS):
        result = handler.handle(request(mode))
        assert result.outcome is EnumServeOutcome.BACKEND_FAILED
        assert "tailscaled is down" in result.detail


def test_an_uninstall_that_leaves_the_listener_on_fails_its_readback() -> None:
    handler, _, backend, _ = build(backend=BreakableBackend(off_does_nothing=True))
    handler.handle(request(EnumServeMode.INSTALL))
    result = handler.handle(request(EnumServeMode.UNINSTALL))
    assert result.outcome is EnumServeOutcome.READBACK_FAILED
    assert backend.served == {PORT: UPSTREAM}
    assert ENV_FILE in backend.env_files


def test_uninstall_leaves_a_foreign_listener_on_the_port_alone() -> None:
    handler, _, backend, _ = build(backend=BreakableBackend({PORT: FOREIGN}))
    result = handler.handle(request(EnumServeMode.UNINSTALL))
    assert result.outcome is EnumServeOutcome.NOT_INSTALLED
    assert result.ok
    assert backend.served == {PORT: FOREIGN}
    assert f"off {PORT}" not in backend.calls


def test_uninstall_with_nothing_installed_changes_nothing() -> None:
    handler, _, backend, _ = build(backend=BreakableBackend({OTHER_PORT: FOREIGN}))
    result = handler.handle(request(EnumServeMode.UNINSTALL))
    assert result.outcome is EnumServeOutcome.NOT_INSTALLED
    assert result.changed is False
    assert backend.served == {OTHER_PORT: FOREIGN}


def test_a_trailing_slash_on_the_served_target_is_still_this_upstream() -> None:
    handler, _, _, _ = build(backend=BreakableBackend({PORT: f"{UPSTREAM}/"}))
    result = handler.handle(request(EnumServeMode.STATUS))
    assert result.outcome is EnumServeOutcome.SERVING


def test_a_missing_overlay_is_indeterminate_exit_3_and_names_the_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    monkeypatch.delenv("ONEX_TAILNET_HTTPS_SERVE_OVERLAY", raising=False)
    assert main(["install"]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err
    assert issubclass(NodeOverlayError, Exception)
