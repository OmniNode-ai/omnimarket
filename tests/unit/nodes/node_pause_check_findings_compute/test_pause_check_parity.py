# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parity tests of the pure pause-check findings node against the pause_check.sh spec.

Each test runs the node's handler over the same ledger rows the spec's
``pause_check.sh`` tests feed the shell script and asserts the same exit code
(3 when the verdict is PAUSED, 0 when CLEAR) and the same stdout markers
(``HOLD 1:``, ``PR-ROW 1:``, ``VERDICT: CLEAR``). The last spec test exercises
the plugin's drain map and stays in the plugin; its node-side counterpart is
``test_repo_scoped_hold_pauses_only_its_repo``.
"""

from __future__ import annotations

import pytest

from omnimarket.models.pause_check import (
    EnumPauseVerdict,
    ModelPauseCheckOverlay,
    ModelPauseCheckPr,
    ModelPauseCheckRequest,
    ModelPauseCheckRules,
)
from omnimarket.nodes.node_pause_check_findings_compute.handlers import (
    HandlerPauseCheckFindings,
)

pytestmark = pytest.mark.unit

NOW = "2026-10-16T22:00:00Z"

# Verbatim ledger row 23214, including its duplicate repo= cell and prose PR reference.
REPO_FIXER_HOLD = (
    "2026-10-15T17:12:23Z | HOLD | lane=orchestrator-bb1a | "
    "id=2026-10-15T17:12:23Z-orchestrator-bb1a | to=all | ticket=OMN-17427 | repo=repo-b | "
    "until=2026-10-31T23:59:00Z | scope=fixer | repo=repo-b | repo-b is parked "
    "until M4 is done (memory ruling: M4 repos only; operator 2026-10-15 ~17:20Z in session: 'why "
    "are we worrying about repo-b when all that work is parked?'). The landing controller "
    "dispatches no fix worker for repo-b and revokes live ones; it had spent opus workers "
    "L8510, L8540, L8551 on repo-b#1902 today. A RELEASE row lifts this."
)


def row(kind: str, fields: str) -> str:
    return f"{NOW} | {kind} | {fields}"


def pause_check(
    rows: list[str],
    *,
    repo: str = "repo-a",
    full_scan: bool = False,
    lane: str = "caller",
) -> tuple[int, str]:
    """Run the node's handler over the ledger rows and return (exit_code, stdout).

    exit_code is 3 when the verdict is PAUSED, else 0; stdout is the rendered
    findings joined by newlines, mirroring the shell script's stdout.
    """
    ledger_text = "\n".join(rows) + "\n"
    request = ModelPauseCheckRequest(
        prs=(ModelPauseCheckPr(repo=repo, number=123, lane=lane, labels=()),),
        rules=ModelPauseCheckRules(
            report_types=(
                "STATUS",
                "TERMINAL",
                "RELEASE",
                "FRICTION",
                "CORRECTION",
                "ACK",
            ),
            landing_order="landing-order",
            parked={},
            repo_pause_lines={},
        ),
        ledger_text=ledger_text,
        now=NOW,
        full_scan=full_scan,
    )
    overlay = ModelPauseCheckOverlay(
        snippet_bytes=220,
        max_order_tokens=8,
        ledger_env="ONEX_PAUSE_CHECK_LEDGER_FILE",
        journal_env="ONEX_PAUSE_CHECK_JOURNAL",
    )
    result = HandlerPauseCheckFindings(overlay).handle(request)
    exit_code = 3 if result.results[0].verdict is EnumPauseVerdict.PAUSED else 0
    stdout = "\n".join(result.results[0].render_lines())
    return exit_code, stdout


@pytest.mark.parametrize("hold_lane", ["peer", "caller"])
@pytest.mark.parametrize("release_owner", ["lane=caller", "lane=peer | from=caller"])
def test_pause_check_own_release_clears_hold(
    hold_lane: str, release_owner: str
) -> None:
    hold = row("HOLD", f"lane={hold_lane} | id=hold-1 | pr=repo-a#123")
    release = row("RELEASE", f"{release_owner} | re=hold-1")

    active_code, active_out = pause_check([hold])
    assert active_code == 3, active_out
    assert "HOLD 1:" in active_out

    cleared_code, cleared_out = pause_check([hold, release])
    assert cleared_code == 0, cleared_out
    assert "VERDICT: CLEAR" in cleared_out
    assert "HOLD 1:" not in cleared_out


@pytest.mark.parametrize("owner", ["lane=caller", "lane=peer | from=caller"])
def test_pause_check_own_msg_is_ignored(owner: str) -> None:
    code, out = pause_check(
        [row("MSG", f"{owner} | to=all | id=msg-1 | hold repo-a#123")]
    )
    assert code == 0, out
    assert "VERDICT: CLEAR" in out
    assert "PR-ROW" not in out


def test_pause_check_peer_msg_still_pauses() -> None:
    code, out = pause_check(
        [row("MSG", "lane=peer | to=all | id=msg-1 | hold repo-a#123")]
    )
    assert code == 3, out
    assert "PR-ROW 1:" in out


@pytest.mark.parametrize("hold_lane", ["peer", "caller"])
def test_pause_check_own_unrelated_release_preserves_hold(hold_lane: str) -> None:
    code, out = pause_check(
        [
            row("HOLD", f"lane={hold_lane} | id=hold-1 | pr=repo-a#123"),
            row("RELEASE", "lane=caller | re=another-hold"),
        ]
    )
    assert code == 3, out
    assert "HOLD 1:" in out


@pytest.mark.parametrize("full_scan", [False, True])
@pytest.mark.parametrize("repo", ["repo-a", "omnimarket", "repo-b"])
def test_repo_scoped_all_lanes_hold(repo: str, full_scan: bool) -> None:
    code, out = pause_check([REPO_FIXER_HOLD], repo=repo, full_scan=full_scan)
    expected = 3 if repo == "repo-b" else 0
    assert code == expected, out
    assert ("HOLD 1:" in out) == (expected == 3)


@pytest.mark.parametrize(
    "scope", ["repo=other", "repos=other", "repo=OmniNode-ai/other"]
)
def test_explicit_other_repo_prevents_lane_fallback(scope: str) -> None:
    for address in ("all", "caller", "peer,caller"):
        code, out = pause_check([row("HOLD", f"id=repo-hold | {scope} | to={address}")])
        assert code == 0, out


@pytest.mark.parametrize("scope", ["", "repo=repo-a", "repos=other,repo-a"])
def test_global_or_matching_repo_hold_still_applies(scope: str) -> None:
    code, out = pause_check([row("HOLD", f"id=hold-1 | to=all | {scope}")])
    assert code == 3, out
    assert "HOLD 1:" in out


def test_repo_scoped_hold_pauses_only_its_repo() -> None:
    """The REPO_FIXER_HOLD row pauses only repo-b, not the other repos."""
    for repo in ("repo-a", "omnimarket"):
        code, out = pause_check([REPO_FIXER_HOLD], repo=repo)
        assert code == 0, out
        assert "HOLD 1:" not in out
    code, out = pause_check([REPO_FIXER_HOLD], repo="repo-b")
    assert code == 3, out
    assert "HOLD 1:" in out
