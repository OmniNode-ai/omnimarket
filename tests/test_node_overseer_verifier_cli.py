# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Focused tests for the node_overseer_verifier CLI."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from omnimarket.nodes.node_overseer_verifier import __main__ as cli


@pytest.mark.unit
def test_cli_pr_populates_claimed_prs_for_live_check(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--pr must feed the live pr_checks_live gate instead of skipping it."""
    captured: list[Any] = []

    class _FakeHandler:
        def verify(self, request: Any) -> dict[str, object]:
            captured.append(request)
            return {
                "verdict": "PASS",
                "checks": [],
                "failure_class": None,
                "summary": "ok",
            }

    monkeypatch.setattr(cli, "HandlerOverseerVerifier", _FakeHandler)
    monkeypatch.setattr(
        sys,
        "argv",
        ["node_overseer_verifier", "--pr", "omnimarket#483", "--json"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert captured
    request = captured[0]
    assert request.task_id == "omnimarket#483"
    assert len(request.claimed_prs) == 1
    assert request.claimed_prs[0].repo == "OmniNode-ai/omnimarket"
    assert request.claimed_prs[0].pr_number == 483
    assert '"verdict": "PASS"' in capsys.readouterr().out


@pytest.mark.unit
def test_cli_pr_preserves_owner_qualified_repo() -> None:
    assert cli._gh_repo("ExampleOrg/example") == "ExampleOrg/example"
