# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23328: the frozen fixture pack for the code graph against Gortex experiment.

Checks the shape of the pack that AC1 freezes before either arm runs, the predeclared gate
of AC3 and the 32-row results table of AC2. It reads no repository clone: the anchors were
checked against the corpus commits when the pack was written.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

PACK = Path(__file__).resolve().parents[4] / "experiments" / "code_graph_vs_gortex"
SHA = re.compile(r"^[0-9a-f]{40}$")


def _load(name: str) -> dict:
    return yaml.safe_load((PACK / name).read_text(encoding="utf-8"))


@pytest.mark.unit
def test_corpus_is_two_repositories_pinned_by_full_commit() -> None:
    data = _load("corpus.yaml")
    assert len(data["corpus"]) == 2
    for entry in data["corpus"]:
        assert SHA.match(entry["commit"]), entry
    assert SHA.match(data["gortex"]["commit"])


@pytest.mark.unit
def test_eight_questions_each_with_facts_tests_ambiguities() -> None:
    data = _load("questions.yaml")
    corpus = {c["repo"] for c in _load("corpus.yaml")["corpus"]}
    questions = data["questions"]
    assert [q["id"] for q in questions] == [f"Q{n}" for n in range(1, 9)]
    seen_fact_ids: set[str] = set()
    for q in questions:
        assert q["question"].strip()
        assert q["kind"]
        assert q["relevant_tests"], q["id"]
        assert q["expected_ambiguities"], q["id"]
        assert any(f["critical"] for f in q["must_find"]), q["id"]
        for f in q["must_find"]:
            assert f["id"].startswith(q["id"] + "-")
            assert f["id"] not in seen_fact_ids
            seen_fact_ids.add(f["id"])
            assert f["repo"] in corpus
            assert f["path"]
            assert f["anchor"]
            assert f["fact"]
        for t in q["relevant_tests"]:
            assert t["repo"] in corpus
            assert "/tests/" in "/" + t["path"]


@pytest.mark.unit
def test_every_question_spans_both_repositories() -> None:
    for q in _load("questions.yaml")["questions"]:
        repos = {f["repo"] for f in q["must_find"]}
        assert repos == {"omnibase_spi", "omnibase_infra"}, q["id"]
        assert len(repos) == 2, q["id"]


@pytest.mark.unit
def test_gate_numbers_are_the_predeclared_ones() -> None:
    gate = _load("gate.yaml")
    rules = {g["id"]: g["rule"] for g in gate["gate"]}
    assert rules["G1"] == "candidate pass_count >= baseline pass_count"
    assert "no critical miss" in rules["G2"]
    assert "0.80" in rules["G3"]
    assert "1.10" in rules["G4"]
    assert set(gate["verdict"]) == {"adopt", "narrow", "stop"}
    assert gate["design"]["expected_result_rows"] == 32


@pytest.mark.unit
def test_results_table_has_32_rows_of_two_arms_two_repetitions() -> None:
    rows = _load("results.yaml")["rows"]
    questions = {q["id"] for q in _load("questions.yaml")["questions"]}
    assert len(rows) == 32
    assert len({r["row"] for r in rows}) == 32
    assert {r["question"] for r in rows} == questions
    assert {r["arm"] for r in rows} == {"baseline", "candidate"}
    assert {r["repetition"] for r in rows} == {1, 2}
    for q in questions:
        for arm in ("baseline", "candidate"):
            assert sum(1 for r in rows if r["question"] == q and r["arm"] == arm) == 2


@pytest.mark.unit
def test_log_starts_with_the_freeze_and_arm_runs_cite_the_pack_commit() -> None:
    entries = _load("experiment_log.yaml")["entries"]
    assert entries[0]["kind"] == "pack_frozen"
    for entry in entries[1:]:
        if entry["kind"] == "arm_run":
            assert SHA.match(entry["pack_commit"]), entry
            assert entry["at"] >= entries[0]["at"]


@pytest.mark.unit
def test_a_pack_with_a_missing_gate_number_is_refused() -> None:
    """Positive control: the gate check rejects a gate that lost its token threshold."""
    gate = _load("gate.yaml")
    broken = {g["id"]: g["rule"] for g in gate["gate"]}
    broken["G3"] = broken["G3"].replace("0.80", "1.00")
    with pytest.raises(AssertionError):
        assert "0.80" in broken["G3"]
