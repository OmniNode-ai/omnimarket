# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A parse check vetoes 'invalid file' findings on files that parse (OMN-17427).

The negative control is the file omnimarket#4063 was blocked for: at head
d25be197 the two lab voters agreed that it "is syntactically invalid Python",
and ``ast.parse`` of it succeeds. The fixture is that file, verbatim.
"""

from __future__ import annotations

import ast
import copy
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_review_finding_veto_effect.__main__ import main
from omnimarket.nodes.node_review_finding_veto_effect.handlers.adapter_git_head import (
    GitHeadFileReader,
)
from omnimarket.nodes.node_review_finding_veto_effect.handlers.handler_review_finding_veto import (
    HandlerReviewFindingVeto,
)
from omnimarket.nodes.node_review_finding_veto_effect.models import (
    ModelReviewFindingVetoRequest,
)

pytestmark = pytest.mark.unit

HEAD = "d25be197856ff515cd1fd1d5e13396f03a54a91f"
PR4063_FILE = "tests/nodes/node_lab_fill_effect/test_golden_chain_lab_fill_tick.py"
PR4063_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "review_finding_veto"
    / "pr4063_head_d25be197_test_golden_chain_lab_fill_tick.py.txt"
)
CLAIM = "Test file is syntactically invalid Python"


class _Reader:
    def __init__(self, files: dict[str, str]) -> None:
        self._files = files

    def read(self, head_sha: str, path: str) -> str | None:
        return self._files.get(path)


def _cluster(
    path: str, message: str, *, ids: tuple[str, ...] = ("a1", "b1")
) -> dict[str, Any]:
    return {
        "file_path": path,
        "line_start": 1,
        "severity": "error",
        "rule": "category:correctness",
        "message": message,
        "agreeing_models": ["correctness", "security-contracts"],
        "agreement_count": 2,
        "blocking": True,
        "finding_ids": list(ids),
    }


def _document(
    *clusters: dict[str, Any], warnings: list[Any] | None = None
) -> dict[str, Any]:
    ids = {i for c in clusters for i in c["finding_ids"]}
    return {
        "models_succeeded": ["correctness", "security-contracts"],
        "results": [
            {
                "model": model,
                "findings": [
                    {"finding_id": i, "normalized_message": c["message"]}
                    for c in clusters
                    for i in c["finding_ids"]
                    if i in ids and i.startswith(prefix)
                ],
            }
            for model, prefix in (("correctness", "a"), ("security-contracts", "b"))
        ],
        "quorum": {
            "verdict": "blocked",
            "blocking_count": len(clusters),
            "warning_count": len(warnings or []),
            "blocking_findings": list(clusters),
            "warning_findings": warnings or [],
        },
    }


def _run(document: dict[str, Any], files: dict[str, str]) -> Any:
    return HandlerReviewFindingVeto(_Reader(files)).handle(
        ModelReviewFindingVetoRequest(result=document, head_sha=HEAD)
    )


def test_pr4063_test_file_parses_at_the_blocked_head() -> None:
    """Positive control: the file the reviewers called invalid parses."""
    ast.parse(PR4063_FIXTURE.read_text(encoding="utf-8"))


def test_pr4063_invalid_python_finding_is_dropped_and_the_gate_passes() -> None:
    text = PR4063_FIXTURE.read_text(encoding="utf-8")
    document = _document(
        _cluster(
            PR4063_FILE,
            f"{CLAIM} | The new test file `{PR4063_FILE}` is not valid Python. It begins "
            "with a fragment and never closes its parenthesis.",
        )
    )
    outcome = _run(document, {PR4063_FILE: text})

    quorum = outcome.result["quorum"]
    assert quorum["blocking_count"] == 0
    assert quorum["blocking_findings"] == []
    assert quorum["verdict"] == "passed"
    assert len(outcome.vetoed) == 1
    assert outcome.vetoed[0].file_path == PR4063_FILE
    assert f"parses as python at {HEAD}" in outcome.vetoed[0].reason
    assert quorum["vetoed_findings"][0]["reason"] == outcome.vetoed[0].reason
    assert document["quorum"]["blocking_count"] == 1, "the input is not mutated"


def test_a_python_file_that_does_not_parse_keeps_its_finding() -> None:
    """Negative control: the veto does not fire when the claim is true."""
    document = _document(_cluster("tests/test_x.py", f"{CLAIM} | line 3 is cut off"))
    outcome = _run(document, {"tests/test_x.py": "def f(:\n    pass\n"})

    assert outcome.vetoed == ()
    assert outcome.result["quorum"]["blocking_count"] == 1
    assert outcome.result["quorum"]["verdict"] == "blocked"
    assert "file does not parse" in outcome.kept_unverified[0]


@pytest.mark.parametrize(
    ("path", "title", "good", "bad"),
    [
        ("config/app.yaml", "Config is invalid YAML", "a: 1\nb: [1, 2]\n", "a: [1\n"),
        ("data/x.json", "Syntax error in JSON manifest", '{"a": [1, 2]}', '{"a": [1,'),
    ],
)
def test_yaml_and_json_claims_are_checked_the_same_way(
    path: str, title: str, good: str, bad: str
) -> None:
    dropped = _run(_document(_cluster(path, f"{title} | detail")), {path: good})
    kept = _run(_document(_cluster(path, f"{title} | detail")), {path: bad})

    assert [v.file_path for v in dropped.vetoed] == [path]
    assert dropped.result["quorum"]["verdict"] == "passed"
    assert kept.vetoed == ()
    assert kept.result["quorum"]["verdict"] == "blocked"


def test_other_finding_classes_are_never_touched() -> None:
    logic = _cluster(PR4063_FILE, "Off-by-one in the staleness check | detail")
    outcome = _run(_document(logic), {PR4063_FILE: PR4063_FIXTURE.read_text("utf-8")})

    assert outcome.vetoed == ()
    assert outcome.kept_unverified == ()
    assert outcome.result["quorum"]["blocking_count"] == 1
    assert outcome.result["quorum"]["verdict"] == "blocked"


def test_another_blocking_finding_keeps_the_verdict_blocked() -> None:
    text = PR4063_FIXTURE.read_text("utf-8")
    syntax = _cluster(PR4063_FILE, f"{CLAIM} | detail", ids=("a1", "b1"))
    real = _cluster(
        "src/x.py", "Shell injection through cli_args | detail", ids=("a2", "b2")
    )
    outcome = _run(_document(syntax, real), {PR4063_FILE: text})

    quorum = outcome.result["quorum"]
    assert quorum["blocking_count"] == 1
    assert quorum["blocking_findings"][0]["file_path"] == "src/x.py"
    assert quorum["verdict"] == "blocked"


def test_warning_findings_are_not_examined() -> None:
    text = PR4063_FIXTURE.read_text("utf-8")
    warning = _cluster(PR4063_FILE, f"{CLAIM} | detail") | {"blocking": False}
    document = _document(warnings=[warning])
    document["quorum"].update(verdict="passed", blocking_count=0)
    outcome = _run(document, {PR4063_FILE: text})

    assert outcome.vetoed == ()
    assert outcome.result == document


def test_a_claim_about_another_file_or_language_is_kept() -> None:
    text = PR4063_FIXTURE.read_text("utf-8")
    other_file = _cluster(PR4063_FILE, "settings.yaml is invalid YAML | detail")
    other_language = _cluster(PR4063_FILE, "Invalid JSON in this module | detail")
    for cluster in (other_file, other_language):
        outcome = _run(_document(cluster), {PR4063_FILE: text})
        assert outcome.vetoed == ()
        assert outcome.result["quorum"]["verdict"] == "blocked"


def test_a_finding_about_text_parsed_at_run_time_is_kept() -> None:
    text = PR4063_FIXTURE.read_text("utf-8")
    cluster = _cluster(PR4063_FILE, "eval of the template raises SyntaxError | detail")
    outcome = _run(_document(cluster), {PR4063_FILE: text})

    assert outcome.vetoed == ()
    assert outcome.result["quorum"]["verdict"] == "blocked"


def test_a_cluster_where_one_voter_raised_something_else_is_kept() -> None:
    text = PR4063_FIXTURE.read_text("utf-8")
    cluster = _cluster(PR4063_FILE, f"{CLAIM} | detail")
    document = _document(cluster)
    document["results"][1]["findings"][0]["normalized_message"] = (
        "Missing await on the claim read | detail"
    )
    outcome = _run(document, {PR4063_FILE: text})

    assert outcome.vetoed == ()
    assert outcome.result["quorum"]["verdict"] == "blocked"


def test_a_file_that_cannot_be_read_keeps_its_finding() -> None:
    outcome = _run(_document(_cluster(PR4063_FILE, f"{CLAIM} | detail")), {})

    assert outcome.vetoed == ()
    assert "not readable" in outcome.kept_unverified[0]
    assert outcome.result["quorum"]["verdict"] == "blocked"


def test_a_result_without_a_quorum_block_passes_through() -> None:
    document = {"models_succeeded": ["a"], "total_findings": 0}
    outcome = _run(copy.deepcopy(document), {})

    assert outcome.result == document
    assert outcome.vetoed == ()


def _git(repo: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *argv],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    (root / "Tests").mkdir(parents=True)
    (root / "Tests" / "Test_Fill.py").write_text(
        PR4063_FIXTURE.read_text("utf-8"), encoding="utf-8"
    )
    (root / "broken.py").write_text("def f(:\n", encoding="utf-8")
    _git(root.parent, "init", "-q", str(root))
    _git(root, "add", "-A")
    _git(
        root,
        "-c", "user.name=t", "-c", "user.email=t@example.com",
        "-c", "commit.gpgsign=false",
        "commit", "-q", "-m", "x",
    )  # fmt: skip
    return root, _git(root, "rev-parse", "HEAD")


def test_git_reader_finds_a_lowercased_path_and_refuses_escapes(
    repo: tuple[Path, str],
) -> None:
    root, sha = repo
    reader = GitHeadFileReader(str(root))

    assert reader.read(sha, "tests/test_fill.py") == PR4063_FIXTURE.read_text("utf-8")
    assert reader.read(sha, "Tests/Test_Fill.py") is not None
    assert reader.read(sha, "../etc/passwd") is None
    assert reader.read(sha, "missing.py") is None
    assert reader.read("not-a-sha", "broken.py") is None


def test_cli_rewrites_the_result_file_and_reports_each_drop(
    repo: tuple[Path, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, sha = repo
    document = _document(
        _cluster("tests/test_fill.py", f"{CLAIM} | detail", ids=("a1", "b1")),
        _cluster("broken.py", f"{CLAIM} | detail", ids=("a2", "b2")),
    )
    result_file = tmp_path / "result.json"
    result_file.write_text(json.dumps(document), encoding="utf-8")

    code = main(
        ["--result-file", str(result_file), "--head-sha", sha, "--repo-root", str(root)]
    )

    written = json.loads(result_file.read_text(encoding="utf-8"))
    err = capsys.readouterr().err
    assert code == 0
    assert [c["file_path"] for c in written["quorum"]["blocking_findings"]] == [
        "broken.py"
    ]
    assert written["quorum"]["verdict"] == "blocked"
    assert "dropped blocking finding tests/test_fill.py:1" in err
    assert "syntax claim stays blocking: broken.py" in err


def test_cli_leaves_the_file_alone_when_it_cannot_be_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result_file = tmp_path / "result.json"
    result_file.write_text("{not json", encoding="utf-8")

    code = main(["--result-file", str(result_file), "--head-sha", HEAD])

    assert code == 2
    assert result_file.read_text(encoding="utf-8") == "{not json"
    assert "not applied" in capsys.readouterr().err


def test_contract_declares_the_handler_and_its_terminal_event() -> None:
    contract = yaml.safe_load(
        (
            Path(__file__).resolve().parents[4]
            / "src/omnimarket/nodes/node_review_finding_veto_effect/contract.yaml"
        ).read_text(encoding="utf-8")
    )

    assert contract["handler"]["class"] == HandlerReviewFindingVeto.__name__
    assert contract["terminal_event"] == (
        "onex.evt.omnimarket.review-finding-veto-completed.v1"
    )
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.review-finding-veto-completed.v1"
    ]
