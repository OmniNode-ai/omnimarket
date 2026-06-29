# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Research task-class quality-gate behavior (OMN-13354).

The `research` task class previously declared the heuristic pair
`cites_specific_lines` + `explains_tradeoffs`. `cites_specific_lines` is a
CODE-line-citation check (`line N` / `Lnn` / `:nn` regex) that a legitimate
research answer cannot satisfy — research cites theorems, papers, and sections,
not code lines. That made the research gate a catch-22: every research output
failed the gate at every tier with `TASK_MISMATCH: missing specific line
citations` (live 2026-06-19 reprove, score 0.733), and the only outputs that
could pass were ones carrying code-line markers, which then passed at the LOCAL
tier and never escalated, so the ceiling could never demonstrate a research
pass.

The fix swaps the research DoD to research-appropriate heuristics:
  * `cites_sources` — general source/reference/theorem/section/page/URL/DOI/
    author-year citation detection (NOT code-line regex).
  * `methodical_analysis` — reasoning-structure markers (because / therefore /
    evidence / risk), replacing the narrow `explains_tradeoffs` keyword form.

OMN-13370 keeps those checks as diagnostics/reject-only pre-filters: they can
reject thin research, but they no longer promote a marker-rich answer to adequate
without the explicit semantic_adequacy authority declared by the research DoD.

These tests pin the acceptance criteria:
  (a) a substantive research answer (cites sources, reasoned, semantically
      complete) passes because semantic_adequacy is declared;
  (b) a thin / empty research answer still FAILS;
  (c) the research path is NOT the code-line-citation regex.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers import (
    handler_quality_gate,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "configs"
    / "task_class_contracts.v1.yaml"
)

# A substantive research answer: cites a named theorem, a section, a bracketed
# numeric reference, an author-year reference, and reasons through the claim
# (because / therefore / evidence). It cites NO code line numbers.
_SUBSTANTIVE_RESEARCH = (
    "The spectral theorem (Theorem 4.1, Reed & Simon, 1980) guarantees that "
    "every self-adjoint compact operator on a separable Hilbert space admits an "
    "orthonormal eigenbasis. According to the proof in Section 3, the eigenvalues "
    "accumulate only at zero; therefore the operator is the norm limit of "
    "finite-rank operators. As shown in [12], the evidence supports the canonical "
    "decomposition, and the risk of dropping compactness is a continuous spectrum."
)

# A second substantive research answer with different citation forms (theorem N,
# equation N, author-year, references keyword) — proves it is not over-fit to one
# marker. Still no code line numbers.
_SUBSTANTIVE_RESEARCH_ALT = (
    "See Theorem 2 and equation 5 in the cited references. According to (Smith, "
    "2021), the number of labeled connected graphs grows asymptotically because "
    "the graph is labeled; therefore the evidence supports the stated bound."
)


def _research_dod() -> tuple[tuple[str, ...], tuple[str, ...]]:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    dod = contract["task_classes"]["research"]["definition_of_done"]
    return (
        tuple(dod.get("deterministic", ())),
        tuple(dod.get("heuristic", ())),
    )


def _score_research(content: str) -> ModelQualityGateInput:
    det, heur = _research_dod()
    return ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="research",
        llm_response_content=content,
        dod_deterministic=det,
        dod_heuristic=heur,
    )


@pytest.mark.unit
def test_substantive_research_answer_passes() -> None:
    """(a) Research passes when explicit semantic adequacy is also present."""
    result = quality_gate_delta(_score_research(_SUBSTANTIVE_RESEARCH))

    assert result.passed
    assert result.fail_category == "pass"
    assert result.quality_score == pytest.approx(1.0)
    assert result.failure_reasons == ()
    assert not any("specific line citations" in r for r in result.failure_reasons), (
        f"code-line-citation reason fired on research: {result.failure_reasons}"
    )


@pytest.mark.unit
def test_second_substantive_research_answer_passes() -> None:
    """(a) Different citation forms also pass with semantic adequacy."""
    result = quality_gate_delta(_score_research(_SUBSTANTIVE_RESEARCH_ALT))

    assert result.passed
    assert result.fail_category == "pass"
    assert result.quality_score == pytest.approx(1.0)
    assert result.failure_reasons == ()


@pytest.mark.unit
def test_thin_research_answer_still_fails() -> None:
    """(b) A thin, unsupported research answer must still FAIL the gate."""
    result = quality_gate_delta(_score_research("Yes, it works fine."))

    assert not result.passed, "thin unsupported answer must not pass the research gate"
    assert result.fail_category == "fail_heuristic"
    assert any("source citations" in r for r in result.failure_reasons), (
        f"thin answer did not trip the cites_sources reason: {result.failure_reasons}"
    )


@pytest.mark.unit
def test_empty_research_answer_still_fails() -> None:
    """(b) An empty research answer must still hard-fail on response_non_empty."""
    result = quality_gate_delta(_score_research("   "))

    assert not result.passed
    assert result.fail_category == "fail_deterministic"


@pytest.mark.unit
def test_code_line_citation_does_not_pass_research() -> None:
    """(c) Code-line-citation content is NOT a research pass.

    Content that ONLY carries code-line markers (`line 42`, `lines 50-55`) — the
    exact form the old `cites_specific_lines` regex accepted — must still fail the
    research gate, proving the research path no longer routes through the
    code-line-citation regex. A research answer is judged on sources + reasoning,
    not code lines.
    """
    result = quality_gate_delta(
        _score_research("The bug is on line 42 and lines 50-55 of the file.")
    )

    assert not result.passed, (
        "code-line-only content must not pass the research gate (it is not a "
        f"research answer): {result.failure_reasons}"
    )
    assert any("source citations" in r for r in result.failure_reasons)


@pytest.mark.unit
def test_research_dod_is_not_the_code_line_citation_check() -> None:
    """(c) The shipped research DoD must declare cites_sources, NOT cites_specific_lines."""
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    heur = contract["task_classes"]["research"]["definition_of_done"]["heuristic"]

    assert "cites_sources" in heur, (
        "research DoD must declare the research-appropriate cites_sources check"
    )
    assert "cites_specific_lines" not in heur, (
        "research DoD must NOT declare the code-line-citation cites_specific_lines "
        "check (OMN-13354 catch-22)"
    )
    assert "methodical_analysis" in heur
    assert "semantic_adequacy" in heur
    assert "explains_tradeoffs" not in heur


@pytest.mark.unit
def test_cites_sources_check_rejects_code_lines_accepts_references() -> None:
    """The cites_sources checker discriminates sources from code lines directly."""
    # A bare code-line citation is NOT a source citation.
    assert (
        handler_quality_gate._check_cites_sources("see line 42 and lines 10-20")
        is not None
    )
    # General research citation forms ARE accepted.
    for cited in (
        "see Theorem 3 for the bound",
        "according to the references below",
        "as shown in [7] this holds",
        "the result (Smith, 2020) is sharp",
        "details at https://example.org/paper",
        "see Section 4 and page 12",
    ):
        assert handler_quality_gate._check_cites_sources(cited) is None, (
            f"cites_sources rejected a valid research citation: {cited!r}"
        )


@pytest.mark.unit
def test_cites_specific_lines_still_code_review_only() -> None:
    """The code-line-citation check stays bound to code-review semantics.

    `cites_specific_lines` must keep accepting code-line markers (it is the
    `review` task class check) and keep rejecting prose without line numbers —
    unchanged by OMN-13354.
    """
    assert handler_quality_gate._check_cites_specific_lines("bug on line 42") is None
    assert (
        handler_quality_gate._check_cites_specific_lines("the function returns a value")
        is not None
    )
