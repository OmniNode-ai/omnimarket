# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Only introduced, evidenced, non-style findings block (OMN-17427).

Every case runs against a scratch git repository, so the classification reads the
real ``git diff``. The recorded findings are the two the gate has produced on
omnimarket: the 'syntactically invalid Python' block on #4063, whose file parses,
and the skip-guard-removal block on #4046, which is a real defect and must keep
blocking. The remaining titles are the style and cleanup classes the rules name.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_review_finding_veto_effect.__main__ import main
from omnimarket.nodes.node_review_finding_veto_effect.handlers.adapter_git_diff import (
    GitDiffReader,
    parse_zero_context_diff,
)
from omnimarket.nodes.node_review_finding_veto_effect.handlers.adapter_git_head import (
    GitHeadFileReader,
)
from omnimarket.nodes.node_review_finding_veto_effect.handlers.handler_review_finding_policy import (
    ROUND_LIMIT,
    HandlerReviewFindingPolicy,
    fingerprint,
)
from omnimarket.nodes.node_review_finding_veto_effect.models import (
    EnumFindingDisposition,
    ModelReviewFindingPolicyRequest,
    ModelReviewRoundState,
)

pytestmark = pytest.mark.unit

TEN = "".join(f"line {i}\n" for i in range(1, 11))
SKIP_GUARD_TITLE = "Skip guard removed from the loader test"
INVALID_PYTHON_TITLE = "Test file is syntactically invalid Python"


def _git(repo: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", *argv],
        check=True, capture_output=True, text=True, timeout=60,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()  # fmt: skip


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


class _Repo:
    """base -> r1 (the head of round three) -> head, on one linear branch."""

    def __init__(self, root: Path) -> None:
        self.root = root
        _git(root, "init", "-q")
        (root / "src").mkdir()
        (root / "src" / "app.py").write_text(TEN, encoding="utf-8")
        (root / "src" / "legacy.py").write_text(TEN, encoding="utf-8")
        self.base = _commit(root, "base")
        # the pull request's first commit changes app.py line 5 and adds new.py
        (root / "src" / "app.py").write_text(
            TEN.replace("line 5\n", "line 5 changed\n"), encoding="utf-8"
        )
        (root / "src" / "new.py").write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
        self.first = _commit(root, "first")
        # its second commit changes app.py line 9 only
        (root / "src" / "app.py").write_text(
            TEN.replace("line 5\n", "line 5 changed\n").replace(
                "line 9\n", "line 9 changed\n"
            ),
            encoding="utf-8",
        )
        self.head = _commit(root, "second")

    def policy(self) -> HandlerReviewFindingPolicy:
        return HandlerReviewFindingPolicy(
            GitHeadFileReader(str(self.root)), GitDiffReader(str(self.root))
        )

    def run(
        self,
        *clusters: dict[str, Any],
        prior: ModelReviewRoundState | None = None,
        head: str | None = None,
    ) -> Any:
        return self.policy().handle(
            ModelReviewFindingPolicyRequest(
                result=_document(*clusters),
                head_sha=head or self.head,
                base_sha=self.base,
                prior_state=prior,
            )
        )


def _cluster(
    path: str,
    line: int,
    title: str,
    *,
    rule: str = "category:logic_error",
) -> dict[str, Any]:
    return {
        "file_path": path,
        "line_start": line,
        "severity": "error",
        "rule": rule,
        "message": f"{title} | detail the voters wrote",
        "agreeing_models": ["a", "b"],
        "agreement_count": 2,
        "blocking": True,
        "finding_ids": ["a1", "b1"],
    }


def _document(*clusters: dict[str, Any]) -> dict[str, Any]:
    return {
        "models_succeeded": ["a", "b"],
        "results": [],
        "quorum": {
            "verdict": "blocked",
            "blocking_count": len(clusters),
            "warning_count": 0,
            "blocking_findings": list(clusters),
            "warning_findings": [],
        },
    }


@pytest.fixture
def repo(tmp_path: Path) -> _Repo:
    return _Repo(tmp_path)


def _dispositions(outcome: Any) -> list[EnumFindingDisposition]:
    return [d.disposition for d in outcome.dropped]


# --- the diff reader ---------------------------------------------------------------


def test_diff_reader_reports_the_new_side_of_each_hunk(repo: _Repo) -> None:
    lines = GitDiffReader(str(repo.root)).changed_lines(
        repo.base, repo.head, from_merge_base=True
    )
    assert lines == {
        "src/app.py": ((5, 5), (9, 9)),
        "src/new.py": ((1, 5),),
    }, "legacy.py is untouched so it is absent"


def test_diff_reader_incremental_range_starts_at_the_previous_head(repo: _Repo) -> None:
    lines = GitDiffReader(str(repo.root)).changed_lines(
        repo.first, repo.head, from_merge_base=False
    )
    assert lines == {"src/app.py": ((9, 9),)}


def test_diff_reader_returns_none_for_a_range_it_cannot_diff(repo: _Repo) -> None:
    assert (
        GitDiffReader(str(repo.root)).changed_lines(
            "0" * 40, repo.head, from_merge_base=True
        )
        is None
    )


def test_a_pure_deletion_hunk_covers_the_lines_around_it() -> None:
    diff = "+++ b/x.py\n@@ -4,2 +3,0 @@\n-gone\n-gone\n+++ /dev/null\n@@ -1 +0,0 @@\n"
    assert parse_zero_context_diff(diff) == {"x.py": ((3, 4),)}


# --- rule 1: classification --------------------------------------------------------


def test_an_important_finding_on_a_line_the_pr_changed_keeps_blocking(
    repo: _Repo,
) -> None:
    """Positive control: the recorded real defect (#4046) on a changed line blocks."""
    outcome = repo.run(_cluster("src/app.py", 5, SKIP_GUARD_TITLE))

    assert outcome.dropped == ()
    assert outcome.kept == 1
    assert outcome.result["quorum"]["verdict"] == "blocked"
    assert outcome.result["quorum"]["blocking_count"] == 1


def test_a_finding_on_a_line_that_exists_at_the_base_is_pre_existing(
    repo: _Repo,
) -> None:
    outcome = repo.run(_cluster("src/app.py", 2, SKIP_GUARD_TITLE))

    assert _dispositions(outcome) == [EnumFindingDisposition.PRE_EXISTING]
    assert "exists at the base" in outcome.dropped[0].reason
    assert outcome.result["quorum"]["verdict"] == "passed"
    assert outcome.result["quorum"]["blocking_count"] == 0
    assert outcome.result["quorum"]["policy_dropped"][0]["disposition"] == (
        "pre_existing"
    )


def test_only_the_pre_existing_finding_is_dropped_when_another_is_introduced(
    repo: _Repo,
) -> None:
    outcome = repo.run(
        _cluster("src/app.py", 2, "Loader ignores a missing file"),
        _cluster("src/app.py", 9, SKIP_GUARD_TITLE),
    )

    assert _dispositions(outcome) == [EnumFindingDisposition.PRE_EXISTING]
    assert outcome.result["quorum"]["verdict"] == "blocked"
    assert [c["line_start"] for c in outcome.result["quorum"]["blocking_findings"]] == [
        9
    ]


def test_a_line_of_a_file_the_pr_adds_is_introduced(repo: _Repo) -> None:
    assert repo.run(_cluster("src/new.py", 3, SKIP_GUARD_TITLE)).dropped == ()


def test_the_4063_invalid_python_claim_on_an_added_file_is_left_to_the_veto(
    repo: _Repo,
) -> None:
    """The policy does not judge a syntax claim; the parse veto owns that class."""
    outcome = repo.run(
        _cluster("src/new.py", 1, INVALID_PYTHON_TITLE, rule="category:correctness")
    )
    assert outcome.dropped == ()


# --- rule 2: evidence --------------------------------------------------------------


def test_a_finding_in_a_file_the_pr_does_not_change_is_refused(repo: _Repo) -> None:
    outcome = repo.run(_cluster("src/legacy.py", 3, SKIP_GUARD_TITLE))

    assert _dispositions(outcome) == [EnumFindingDisposition.NO_EVIDENCE]
    assert "does not change that file" in outcome.dropped[0].reason


def test_a_finding_that_names_no_file_is_refused(repo: _Repo) -> None:
    outcome = repo.run(_cluster("", 3, SKIP_GUARD_TITLE))
    assert _dispositions(outcome) == [EnumFindingDisposition.NO_EVIDENCE]
    assert "names no file" in outcome.dropped[0].reason


def test_a_line_past_the_end_of_the_file_is_refused(repo: _Repo) -> None:
    outcome = repo.run(_cluster("src/new.py", 400, SKIP_GUARD_TITLE))
    assert _dispositions(outcome) == [EnumFindingDisposition.NO_EVIDENCE]
    assert "past the end of the file" in outcome.dropped[0].reason


def test_a_finding_without_a_line_is_refused(repo: _Repo) -> None:
    cluster = _cluster("src/app.py", 5, SKIP_GUARD_TITLE)
    del cluster["line_start"]
    assert _dispositions(repo.run(cluster)) == [EnumFindingDisposition.NO_EVIDENCE]


def test_a_lower_cased_path_with_a_location_suffix_still_resolves(repo: _Repo) -> None:
    """The quorum lower-cases paths and a model may append ':line' to one."""
    outcome = repo.run(_cluster("SRC/App.py:5", 5, SKIP_GUARD_TITLE))
    assert outcome.dropped == ()


# --- rule 3: style and cleanup classes never block ----------------------------------


@pytest.mark.parametrize(
    ("rule", "title"),
    [
        ("category:style", "Inconsistent brace placement"),
        ("category:informational", "Worth knowing about the loader"),
        ("category:correctness", "Unused import of json"),
        ("category:correctness", "Variable naming does not follow the module"),
        ("category:correctness", "Docstring is missing a Returns section"),
        ("category:correctness", "Missing type annotations on the helper"),
        ("category:correctness", "Typo in the comment"),
        ("category:correctness", "Consider using a dataclass here"),
        ("category:correctness", "Line too long"),
    ],
)
def test_style_and_cleanup_findings_on_a_changed_line_do_not_block(
    repo: _Repo, rule: str, title: str
) -> None:
    outcome = repo.run(_cluster("src/app.py", 5, title, rule=rule))

    assert _dispositions(outcome) == [EnumFindingDisposition.NIT]
    assert outcome.result["quorum"]["verdict"] == "passed"


@pytest.mark.parametrize(
    ("rule", "title"),
    [
        ("category:security", "Rename of the token variable exposes it"),
        ("category:logic_error", "Unused import hides a failing branch"),
        ("category:contract_breach", "Docstring contract no longer holds"),
        ("category:correctness", "Unused guard silently skips the failure path"),
        ("category:correctness", "Skip guard removal makes the test pass on failure"),
        ("category:correctness", "Redundant check removed, so null input crashes"),
    ],
)
def test_a_real_defect_is_never_read_as_a_nit(
    repo: _Repo, rule: str, title: str
) -> None:
    """Negative control: cleanup words in a defect's title do not drop it."""
    outcome = repo.run(_cluster("src/app.py", 5, title, rule=rule))

    assert outcome.dropped == ()
    assert outcome.result["quorum"]["verdict"] == "blocked"


@pytest.mark.parametrize("rule", ["standing:no-hardcoded-topics", "rules-file:invalid"])
def test_a_standing_rule_finding_is_judged_by_neither_line_nor_class(
    repo: _Repo, rule: str
) -> None:
    """The reviewer anchors these at line 1 of a file; they are rules, not lines."""
    outcome = repo.run(_cluster("src/legacy.py", 1, "Naming rule weakened", rule=rule))

    assert outcome.dropped == ()
    assert outcome.result["quorum"]["verdict"] == "blocked"


# --- rule 4: rounds ----------------------------------------------------------------


def _state(
    repo: _Repo, *, round_number: int, head: str, posted: tuple[str, ...] = ()
) -> ModelReviewRoundState:
    return ModelReviewRoundState(
        round=round_number, head=head, previous_head=None, posted=posted
    )


def test_a_first_review_is_round_one_and_records_what_it_shows(repo: _Repo) -> None:
    cluster = _cluster("src/app.py", 5, SKIP_GUARD_TITLE)
    outcome = repo.run(cluster)

    assert outcome.state.round == 1
    assert outcome.result["quorum"]["review_round"] == 1
    assert outcome.state.posted == (
        fingerprint("src/app.py", "category:logic_error", cluster["message"]),
    )
    assert outcome.state.posted_before == ()


def test_each_new_head_is_a_new_round_and_a_rerun_of_one_head_is_not(
    repo: _Repo,
) -> None:
    again = repo.run(
        _cluster("src/app.py", 5, SKIP_GUARD_TITLE),
        prior=_state(repo, round_number=2, head=repo.first),
    )
    assert again.state.round == 3
    assert again.state.previous_head == repo.first

    rerun = repo.run(
        _cluster("src/app.py", 5, SKIP_GUARD_TITLE),
        prior=again.state,
    )
    assert rerun.state.round == 3
    assert rerun.state.previous_head == repo.first


def test_a_finding_posted_in_round_three_still_blocks_in_round_three(
    repo: _Repo,
) -> None:
    """Negative control: the repeat rule does not start before the limit."""
    cluster = _cluster("src/app.py", 5, SKIP_GUARD_TITLE)
    posted = fingerprint("src/app.py", cluster["rule"], cluster["message"])
    outcome = repo.run(
        cluster,
        prior=_state(
            repo, round_number=ROUND_LIMIT - 1, head=repo.first, posted=(posted,)
        ),
    )

    assert outcome.state.round == ROUND_LIMIT
    assert outcome.dropped == ()
    assert outcome.result["quorum"]["verdict"] == "blocked"


def test_after_three_rounds_a_posted_finding_is_not_repeated(repo: _Repo) -> None:
    cluster = _cluster("src/app.py", 9, SKIP_GUARD_TITLE)
    posted = fingerprint("src/app.py", cluster["rule"], cluster["message"])
    outcome = repo.run(
        cluster,
        prior=_state(repo, round_number=ROUND_LIMIT, head=repo.first, posted=(posted,)),
    )

    assert outcome.state.round == ROUND_LIMIT + 1
    assert _dispositions(outcome) == [EnumFindingDisposition.REPEAT]
    assert outcome.result["quorum"]["verdict"] == "passed"


def test_a_posted_finding_moved_to_another_line_is_still_a_repeat(repo: _Repo) -> None:
    posted_cluster = _cluster("src/app.py", 5, SKIP_GUARD_TITLE)
    posted = fingerprint(
        "src/app.py", posted_cluster["rule"], posted_cluster["message"]
    )
    outcome = repo.run(
        _cluster("src/app.py", 9, SKIP_GUARD_TITLE),
        prior=_state(repo, round_number=ROUND_LIMIT, head=repo.first, posted=(posted,)),
    )
    assert _dispositions(outcome) == [EnumFindingDisposition.REPEAT]


def test_after_three_rounds_only_the_diff_since_the_previous_round_is_reviewed(
    repo: _Repo,
) -> None:
    old = _cluster("src/app.py", 5, "Loader reads the wrong file")  # round one's change
    new = _cluster("src/app.py", 9, "Loader reads a stale cache")  # since `first`
    outcome = repo.run(
        old, new, prior=_state(repo, round_number=ROUND_LIMIT, head=repo.first)
    )

    assert _dispositions(outcome) == [EnumFindingDisposition.ALREADY_REVIEWED]
    assert outcome.dropped[0].line_start == 5
    assert [c["line_start"] for c in outcome.result["quorum"]["blocking_findings"]] == [
        9
    ]
    assert outcome.result["quorum"]["verdict"] == "blocked"


def test_a_rerun_of_a_head_past_the_limit_does_not_repeat_its_own_findings(
    repo: _Repo,
) -> None:
    cluster = _cluster("src/app.py", 9, SKIP_GUARD_TITLE)
    first_run = repo.run(
        cluster, prior=_state(repo, round_number=ROUND_LIMIT, head=repo.first)
    )
    assert first_run.state.round == ROUND_LIMIT + 1
    assert first_run.dropped == ()

    rerun = repo.run(cluster, prior=first_run.state)

    assert rerun.state.round == ROUND_LIMIT + 1
    assert rerun.dropped == (), "a rerun of the same head is not a second round"
    assert rerun.result["quorum"]["verdict"] == "blocked"


def test_an_unreachable_previous_head_still_suppresses_posted_findings(
    repo: _Repo,
) -> None:
    gone = "f" * 40
    cluster = _cluster("src/app.py", 9, SKIP_GUARD_TITLE)
    fresh = _cluster("src/app.py", 5, "Loader reads the wrong file")
    posted = fingerprint("src/app.py", cluster["rule"], cluster["message"])
    outcome = repo.run(
        cluster,
        fresh,
        prior=_state(repo, round_number=ROUND_LIMIT, head=gone, posted=(posted,)),
    )

    assert _dispositions(outcome) == [EnumFindingDisposition.REPEAT]
    assert [c["line_start"] for c in outcome.result["quorum"]["blocking_findings"]] == [
        5
    ]


def test_standing_rule_findings_are_never_suppressed_by_a_round(repo: _Repo) -> None:
    cluster = _cluster("src/legacy.py", 1, "Rule weakened", rule="standing:r1")
    posted = fingerprint("src/legacy.py", "standing:r1", cluster["message"])
    outcome = repo.run(
        cluster,
        prior=_state(repo, round_number=ROUND_LIMIT, head=repo.first, posted=(posted,)),
    )
    assert outcome.dropped == ()
    assert outcome.result["quorum"]["verdict"] == "blocked"


# --- fail-safe ---------------------------------------------------------------------


def test_an_unreadable_diff_leaves_every_finding_blocking(repo: _Repo) -> None:
    outcome = repo.policy().handle(
        ModelReviewFindingPolicyRequest(
            result=_document(_cluster("src/legacy.py", 3, SKIP_GUARD_TITLE)),
            head_sha=repo.head,
            base_sha="0" * 40,
        )
    )
    assert outcome.dropped == ()
    assert outcome.result["quorum"]["verdict"] == "blocked"


def test_a_document_with_no_quorum_is_returned_untouched(repo: _Repo) -> None:
    outcome = repo.policy().handle(
        ModelReviewFindingPolicyRequest(
            result={"models_succeeded": []}, head_sha=repo.head, base_sha=repo.base
        )
    )
    assert outcome.result == {"models_succeeded": []}
    assert outcome.dropped == ()


def test_the_input_document_is_not_mutated(repo: _Repo) -> None:
    document = _document(_cluster("src/app.py", 2, SKIP_GUARD_TITLE))
    repo.policy().handle(
        ModelReviewFindingPolicyRequest(
            result=document, head_sha=repo.head, base_sha=repo.base
        )
    )
    assert document["quorum"]["blocking_count"] == 1


# --- the entry point the workflow runs ---------------------------------------------


def _main(repo: _Repo, tmp_path: Path, document: dict[str, Any], *extra: str) -> int:
    result_file = tmp_path / "result.json"
    result_file.write_text(json.dumps(document), encoding="utf-8")
    return main(
        [
            "--result-file", str(result_file),
            "--head-sha", repo.head,
            "--repo-root", str(repo.root),
            *extra,
        ]
    )  # fmt: skip


def test_the_entry_point_applies_the_policy_and_writes_the_report(
    repo: _Repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    document = _document(
        _cluster("src/app.py", 2, "Loader ignores a missing file"),
        _cluster("src/app.py", 9, SKIP_GUARD_TITLE),
    )
    code = _main(
        repo,
        out,
        document,
        "--base-sha",
        repo.base,
        "--report-out",
        str(out / "report.json"),
    )

    assert code == 0
    rewritten = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert rewritten["quorum"]["blocking_count"] == 1
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["state"]["round"] == 1
    assert report["kept"] == [f"src/app.py:9 {SKIP_GUARD_TITLE}"]
    assert [d["disposition"] for d in report["dropped"]] == ["pre_existing"]
    assert "dropped blocking finding (pre_existing)" in capsys.readouterr().err


def test_the_entry_point_reads_the_prior_state_and_ignores_a_corrupt_one(
    repo: _Repo, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    prior = out / "prior.json"
    prior.write_text("{not json", encoding="utf-8")
    document = _document(_cluster("src/app.py", 9, SKIP_GUARD_TITLE))
    assert (
        _main(
            repo, out, document, "--base-sha", repo.base,
            "--prior-state-file", str(prior), "--report-out", str(out / "report.json"),
        )
        == 0
    )  # fmt: skip
    assert (
        json.loads((out / "report.json").read_text(encoding="utf-8"))["state"]["round"]
        == 1
    )

    prior.write_text(
        _state(repo, round_number=4, head=repo.first).model_dump_json(),
        encoding="utf-8",
    )
    _main(
        repo, out, _document(_cluster("src/app.py", 9, SKIP_GUARD_TITLE)),
        "--base-sha", repo.base, "--prior-state-file", str(prior),
        "--report-out", str(out / "report.json"),
    )  # fmt: skip
    assert (
        json.loads((out / "report.json").read_text(encoding="utf-8"))["state"]["round"]
        == 5
    )


def test_without_a_base_sha_the_entry_point_applies_no_policy(
    repo: _Repo, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    document = _document(_cluster("src/legacy.py", 3, SKIP_GUARD_TITLE))
    assert _main(repo, out, document) == 0
    rewritten = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert rewritten["quorum"]["verdict"] == "blocked"
