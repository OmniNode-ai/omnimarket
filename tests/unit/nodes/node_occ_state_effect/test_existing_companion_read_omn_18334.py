# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18334: the read-EFFECT resolves the companion that already exists.

The repair the compute performs -- restoring an evidence line a description
rewrite dropped -- needs to know WHICH companion the body should name. This
module pins how that is resolved: by the deterministic companion branch, which
is one-to-one with the product pull request, never by searching for the ticket,
which would collide with every other pull request citing the same ticket.

The failure direction is pinned too. A read that fails degrades to the born path
rather than refusing, because the cost of a missed repair is a line a later run
restores and the cost of a refusal is a companion that never mints at all.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.events.occ_companion import companion_branch_for
from omnimarket.github_api import GitHubApiError
from omnimarket.nodes.node_occ_state_effect.handlers import handler_occ_state_effect
from omnimarket.nodes.node_occ_state_effect.handlers.handler_occ_state_effect import (
    HandlerOccStateEffect,
)

pytestmark = pytest.mark.unit

_OCC = "OmniNode-ai/onex_change_control"
_PRODUCT = "OmniNode-ai/omnimarket"
_PR = 1760


def _resolve(monkeypatch: pytest.MonkeyPatch, result: Any) -> Any:
    seen: list[str] = []

    def _fake(method: str, path: str, token: str = "", **kw: object) -> Any:
        seen.append(path)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(handler_occ_state_effect, "rest_json_array", _fake)
    handler = HandlerOccStateEffect()
    found = handler._existing_companion(_OCC, _PRODUCT, _PR, "fixture-token")
    return found, seen


def test_the_branch_is_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    found, seen = _resolve(
        monkeypatch, [{"number": 4242, "state": "open", "merged": False}]
    )
    assert found is not None
    assert found.pr_number == 4242
    assert found.head_branch == companion_branch_for(_PRODUCT, _PR)
    assert len(seen) == 1
    assert f"head=OmniNode-ai:{companion_branch_for(_PRODUCT, _PR)}" in seen[0], (
        "the companion is resolved by the branch that is one-to-one with "
        "this pull request, never by a ticket search"
    )
    assert "state=all" in seen[0], (
        "a MERGED companion is exactly the one whose binding must be restored, "
        "and GitHub reports it as closed"
    )


def test_a_merged_companion_is_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    found, _ = _resolve(
        monkeypatch,
        [{"number": 4242, "state": "closed", "merged_at": "2026-09-01T00:00:00Z"}],
    )
    assert found is not None
    assert found.merged is True
    assert found.state == "closed"


def test_the_newest_companion_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    found, _ = _resolve(
        monkeypatch,
        [
            {"number": 4242, "state": "closed", "merged": False},
            {"number": 4999, "state": "open", "merged": False},
        ],
    )
    assert found is not None
    assert found.pr_number == 4999


def test_no_companion_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    found, _ = _resolve(monkeypatch, [])
    assert found is None


def test_a_failed_read_degrades_to_the_born_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found, _ = _resolve(monkeypatch, GitHubApiError("boom", status_code=502))
    assert found is None, (
        "a failed read must degrade to the born path, not refuse the mint"
    )


def test_a_malformed_row_is_not_a_companion(monkeypatch: pytest.MonkeyPatch) -> None:
    found, _ = _resolve(monkeypatch, [{"state": "open"}])
    assert found is None
