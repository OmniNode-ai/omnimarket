# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20161 -- the OCC writer app is exempt from the companion-merged gate
ONLY when the producer's head-SHA-bound outcome says dependency-pin-only.

``DEPENDENCY_BOT_AUTHORS`` stays the unconditional list (dependabot, renovate).
``OCC_WRITER_BOT_AUTHORS`` is a separate set meaning "exempt only when pin-only
is proven", proven by the file's own outcome reader and its own
``is_no_companion_required`` predicate. Every other state fails closed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts.ci import check_occ_companion_merged as gate
from scripts.ci.check_occ_companion_merged import (
    AUTOBIND_NO_COMPANION_REQUIRED_REASONS,
    AUTOBIND_OUTCOME_CHECK_NAME,
    AUTOBIND_OUTCOME_MARKER_PREFIX,
    DEPENDENCY_BOT_AUTHORS,
    EXIT_PASS,
    OCC_WRITER_BOT_AUTHORS,
    Verdict,
    evaluate_once,
)

pytestmark = pytest.mark.unit

REPO = "OmniNode-ai/omnimarket"
HEAD = "4db59ba8c73e78cc605e8a3f0291009d5566cc76"
OTHER_HEAD = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
PIN_ONLY = "skip:DEPENDENCY_PIN_ONLY — no companion required (pin-only)"
NO_RED = "skip:NO_RED_DERIVABLE_CHECK — hand-authored evidence is required"
WRITER_LOGINS = (
    "app/onexbot-occ-writer",
    "onexbot-occ-writer[bot]",
    "onexbot-occ-writer",
)
NEAR_MISS = (
    "onexbot-occ-writer-fork",
    "app/onexbot-occ-writerx",
    "xonexbot-occ-writer",
    "onexbot-occ-writer[bot]x",
    "",
)


def _run(outcome: str, reason: str) -> dict[str, object]:
    summary = (
        f"{AUTOBIND_OUTCOME_MARKER_PREFIX} {outcome} repo={REPO} pr=1 "
        f"correlation_id=c reason={reason}\n"
    )
    return {
        "name": AUTOBIND_OUTCOME_CHECK_NAME,
        "status": "completed",
        "completed_at": "2026-09-30T16:00:00Z",
        "output": {"title": outcome, "summary": summary},
    }


class Fetcher:
    def __init__(
        self,
        *,
        author: str,
        runs: dict[str, list[dict[str, object]] | None] | None = None,
        body: str = "no evidence yet",
    ) -> None:
        self.pr = {"body": body, "author": {"login": author}, "headRefOid": HEAD}
        self.runs = runs or {}
        self.check_runs_calls: list[str] = []

    def pr_view(self, repo: str, number: str, fields: str) -> dict[str, object] | None:
        return self.pr

    def compare_status(self, repo: str, base: str, head_sha: str) -> str | None:
        return "behind"

    def check_runs(self, repo: str, head_sha: str) -> list[dict[str, object]] | None:
        self.check_runs_calls.append(head_sha)
        return self.runs.get(head_sha, [])


def _evaluate(fetcher: Fetcher) -> Verdict:
    kwargs: dict[str, Any] = {
        "event_name": "pull_request",
        "repo": REPO,
        "pr_number": "1",
    }
    return evaluate_once(fetcher, **kwargs)  # type: ignore[arg-type]


class TestSets:
    def test_writer_set_is_exactly_the_three_logins(self) -> None:
        assert frozenset(WRITER_LOGINS) == OCC_WRITER_BOT_AUTHORS

    def test_dependency_set_is_unchanged_and_disjoint(self) -> None:
        assert (
            frozenset(
                {
                    "dependabot[bot]",
                    "app/dependabot",
                    "dependabot",
                    "renovate[bot]",
                    "app/renovate",
                    "renovate",
                }
            )
            == DEPENDENCY_BOT_AUTHORS
        )
        assert not (DEPENDENCY_BOT_AUTHORS & OCC_WRITER_BOT_AUTHORS)

    def test_token_parity_with_the_producer(self) -> None:
        """The reader's token is the one the producer writes (in-repo check)."""
        emitter = (
            Path(__file__).resolve().parents[4]
            / "src/omnimarket/nodes/node_pr_lifecycle_fix_effect/handlers"
            / "occ_companion_emitter.py"
        ).read_text()
        assert AUTOBIND_NO_COMPANION_REQUIRED_REASONS == ("skip:DEPENDENCY_PIN_ONLY",)
        for token in AUTOBIND_NO_COMPANION_REQUIRED_REASONS:
            assert token in emitter


@pytest.mark.parametrize("login", WRITER_LOGINS)
class TestWriterApp:
    def test_pin_only_on_current_head_is_exempt(self, login: str) -> None:
        f = Fetcher(author=login, runs={HEAD: [_run("DECLINED", PIN_ONLY)]})
        v = _evaluate(f)
        assert v.code == EXIT_PASS
        assert "dependency-pin-only" in v.reason
        assert f.check_runs_calls == [HEAD]

    def test_exempt_even_when_a_stamp_is_already_cited(self, login: str) -> None:
        f = Fetcher(
            author=login,
            runs={HEAD: [_run("DECLINED", PIN_ONLY)]},
            body="Evidence-Source: OCC#99999",
        )
        assert _evaluate(f).code == EXIT_PASS

    @pytest.mark.parametrize(
        "runs",
        [
            {HEAD: [_run("MINTED", "")]},
            {HEAD: [_run("DECLINED", NO_RED)]},
            {HEAD: [_run("ERROR", PIN_ONLY)]},
            {HEAD: []},
            {HEAD: None},
            {OTHER_HEAD: [_run("DECLINED", PIN_ONLY)]},
        ],
        ids=["minted", "other-reason", "error", "absent", "unreadable", "other-sha"],
    )
    def test_anything_else_is_not_exempt(
        self, login: str, runs: dict[str, list[dict[str, object]] | None]
    ) -> None:
        plain = Fetcher(author="human", runs=runs)
        writer = Fetcher(author=login, runs=runs)
        assert _evaluate(writer) == _evaluate(plain)
        assert _evaluate(writer).code != EXIT_PASS

    def test_a_stamp_still_evaluated_when_not_exempt(self, login: str) -> None:
        f = Fetcher(author=login, runs={HEAD: [_run("MINTED", "")]}, body="")
        assert _evaluate(f) == _evaluate(Fetcher(author="human", runs=f.runs, body=""))


class TestOthers:
    @pytest.mark.parametrize("login", ["human", *NEAR_MISS])
    def test_human_and_near_miss_never_reach_the_probe(self, login: str) -> None:
        # A cited stamp means the pre-existing outcome short-circuit is not
        # consulted, so any read of check-runs would be the exemption probe.
        f = Fetcher(
            author=login,
            runs={HEAD: [_run("DECLINED", PIN_ONLY)]},
            body="Evidence-Source: OCC#99999",
        )
        assert _evaluate(f).code != EXIT_PASS
        assert f.check_runs_calls == []

    @pytest.mark.parametrize("login", NEAR_MISS)
    def test_near_miss_behaves_like_a_human(self, login: str) -> None:
        runs: dict[str, list[dict[str, object]] | None] = {
            HEAD: [_run("DECLINED", NO_RED)]
        }
        assert _evaluate(Fetcher(author=login, runs=runs)) == _evaluate(
            Fetcher(author="human", runs=runs)
        )

    @pytest.mark.parametrize("login", sorted(DEPENDENCY_BOT_AUTHORS))
    def test_dependency_bots_exempt_without_probe(self, login: str) -> None:
        f = Fetcher(author=login)
        assert _evaluate(f).code == EXIT_PASS
        assert f.check_runs_calls == []

    def test_the_token_has_one_spelling_in_the_script(self) -> None:
        src = Path(gate.__file__).read_text()
        assert src.count('"skip:DEPENDENCY_PIN_ONLY"') == 1
