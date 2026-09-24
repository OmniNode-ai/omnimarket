# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18932 (K5 of OMN-18925): the D1 output-only release-acceptance bar.

Unit controls for :mod:`omnimarket.delegation.output_only_acceptance` and for
the live conformance runner that reads it. The runtime-path fixtures (a
polluted response that extraction and the gate both pass, then refused by the
bar) live in ``test_reasoning_preamble_veto_omn18379.py``, the file K5's
change-control contract runs.

The runner controls started as an uncommitted Codex draft for this ticket
(``tests/unit/delegation/test_output_only_acceptance_omn18932.py`` on branch
``codex/omn-18932-output-only-acceptance``, 2026-09-20). They are carried
forward here against the runner's current terminal fields (``response``,
``quality_gate_passed``, ``provider``, ``model_name``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from omnibase_core.models.delegation.wire import EnumDelegationOutputShape

from omnimarket.delegation import response_contract_conformance_runner as runner
from omnimarket.delegation.deliverable_extraction import (
    EnumDeliverableBoundaryMode,
    ModelDeliverableContract,
    ModelPlainTextOutputConstraints,
    resolve_deliverable_contract,
)
from omnimarket.delegation.output_only_acceptance import (
    EnumOutputOnlyEvidenceBasis,
    EnumOutputOnlyRefusal,
    ModelOutputOnlyVerdict,
    evaluate_output_only,
)
from omnimarket.inference.task_class_authority import load_task_class_authority

pytestmark = pytest.mark.unit

_MARKDOWN = resolve_deliverable_contract({"x-omninode-output-shape": "markdown"})
_ARTIFACT = "## Result\n\nThe requested deliverable."


def _verdict(raw: str | None, caller: str) -> ModelOutputOnlyVerdict:
    return evaluate_output_only(
        raw_response=raw, caller_bytes=caller, contract=_MARKDOWN
    )


def test_artifact_only_raw_response_is_accepted() -> None:
    verdict = _verdict(_ARTIFACT, _ARTIFACT)
    assert verdict.accepted is True
    assert verdict.raw_chars == verdict.caller_chars == len(_ARTIFACT)


def test_the_declared_render_start_marker_is_the_only_permitted_leading_text() -> None:
    assert _verdict("### ANSWER\n" + _ARTIFACT, _ARTIFACT).accepted is True
    refused = _verdict("=== ANSWER ===\n" + _ARTIFACT, _ARTIFACT)
    assert refused.refusals == (EnumOutputOnlyRefusal.EXTRACTION_REQUIRED_LEADING_TEXT,)


def test_caller_bytes_that_are_not_a_raw_slice_are_refused() -> None:
    verdict = _verdict(_ARTIFACT, _ARTIFACT.replace("requested", "rewritten"))
    assert EnumOutputOnlyRefusal.CALLER_BYTES_NOT_A_RAW_SLICE in verdict.refusals


def test_a_raw_response_that_repeats_the_answer_is_refused() -> None:
    """The accepted form is exact, so a second copy is extra text, not a match."""
    verdict = _verdict(_ARTIFACT + "\n\n" + _ARTIFACT, _ARTIFACT)
    assert verdict.refusals == (
        EnumOutputOnlyRefusal.EXTRACTION_REQUIRED_TRAILING_TEXT,
    )


def test_an_answer_embedded_mid_response_is_refused_on_both_sides() -> None:
    verdict = _verdict("Draft follows.\n" + _ARTIFACT + "\nEnd of draft.", _ARTIFACT)
    assert verdict.refusals == (
        EnumOutputOnlyRefusal.EXTRACTION_REQUIRED_LEADING_TEXT,
        EnumOutputOnlyRefusal.EXTRACTION_REQUIRED_TRAILING_TEXT,
    )


def test_the_marker_must_stand_on_its_own_line() -> None:
    verdict = _verdict("### ANSWER " + _ARTIFACT, _ARTIFACT)
    assert verdict.accepted is False


def test_declared_lead_in_inside_the_caller_bytes_is_planning_prose() -> None:
    caller = "Okay, let me write it.\n\n" + _ARTIFACT
    verdict = _verdict(caller, caller)
    assert verdict.refusals == (EnumOutputOnlyRefusal.PLANNING_PROSE,)


def test_a_reasoning_terminator_inside_the_caller_bytes_is_planning_prose() -> None:
    caller = "thinking</think>\n" + _ARTIFACT
    verdict = _verdict(caller, caller)
    assert EnumOutputOnlyRefusal.PLANNING_PROSE in verdict.refusals


def test_a_declared_phrase_mid_document_is_not_a_trailing_self_review() -> None:
    """Negative control: the opener is matched only at the final paragraph's start."""
    caller = "## Contact\n\nLet me know is the team's intake form.\n\nThe form is open."
    assert _verdict(caller, caller).accepted is True


def test_every_declared_self_review_opener_is_refused_as_a_final_paragraph() -> None:
    policy = load_task_class_authority().output_only_acceptance
    assert policy is not None
    for opener in policy.trailing_self_review_openers:
        caller = f"{_ARTIFACT}\n\n{opener.capitalize()} anything else you need."
        verdict = _verdict(caller, caller)
        assert verdict.refusals == (EnumOutputOnlyRefusal.TRAILING_SELF_REVIEW,), opener


def test_empty_caller_bytes_are_refused() -> None:
    verdict = _verdict("   \n", "")
    assert verdict.refusals == (EnumOutputOnlyRefusal.EMPTY_DELIVERABLE,)


def test_plain_text_rejects_a_fence_and_honours_declared_word_limits() -> None:
    contract = ModelDeliverableContract(
        output_shape=EnumDelegationOutputShape.PLAIN_TEXT,
        min_deliverable_share=0.5,
        boundary_mode=EnumDeliverableBoundaryMode.FINAL_PARAGRAPH,
        plain_text_constraints=ModelPlainTextOutputConstraints(
            min_words=2, max_words=4
        ),
    )
    fenced = "```\nx\n```"
    fence_verdict = evaluate_output_only(
        raw_response=fenced, caller_bytes=fenced, contract=contract
    )
    assert EnumOutputOnlyRefusal.MALFORMED_STRUCTURE in fence_verdict.refusals
    long = "one two three four five"
    assert evaluate_output_only(
        raw_response=long, caller_bytes=long, contract=contract
    ).refusals == (EnumOutputOnlyRefusal.MALFORMED_STRUCTURE,)
    short = "one two three"
    assert evaluate_output_only(
        raw_response=short, caller_bytes=short, contract=contract
    ).accepted


def test_verdict_cannot_be_constructed_accepted_with_a_refusal() -> None:
    with pytest.raises(ValueError, match="accepted must be true exactly"):
        ModelOutputOnlyVerdict(
            accepted=True,
            refusals=(EnumOutputOnlyRefusal.EMPTY_DELIVERABLE,),
            details=("x",),
            output_shape=_MARKDOWN.output_shape,
            evidence_basis=EnumOutputOnlyEvidenceBasis.ABSENT,
            caller_sha256="0" * 64,
            caller_chars=0,
            raw_sha256=None,
        )


# ---------------------------------------------------------------------------
# The live conformance runner judges the caller-returned bytes, never an alias.
# Carried forward from the Codex draft for this ticket; see the module docstring.
# ---------------------------------------------------------------------------

_PROMPT = "Return exactly the requested Markdown artifact, with no other text."


def _manifest() -> dict[str, object]:
    return {
        "manifest_id": "omn18932-synthetic-output-only-control",
        "local_only": True,
        "contracts": [
            {
                "contract_id": "synthetic-exact-markdown-artifact",
                "task_type": "document",
                "expected_model": "Qwen3.8-27B",
                "prompt": _PROMPT,
                "output_shape": "markdown",
                # Deliberately lax: the pattern alone would pass any response.
                "returned_content_pattern": r"(?s).*" + re.escape(_ARTIFACT) + r".*",
                "minimum_pass_rate": 1.0,
                "response_contract": {"x-omninode-output-shape": "markdown"},
                "markers": [],
                "trials": 1,
            }
        ],
    }


def _run_terminal_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    returned_content: str,
    *,
    cleaned_alias: str | None = None,
    preamble_chars: int = 0,
) -> dict[str, object]:
    resolved = runner.resolve_task_class_deliverable_contract(
        "document", {"x-omninode-output-shape": "markdown"}
    )
    terminal: dict[str, object] = {
        "run_id": "synthetic-omn18932-run",
        "response": returned_content,
        "preamble_chars": preamble_chars,
        "quality_gate_passed": True,
        "provider": "local",
        "model_name": "Qwen3.8-27B",
        "response_contract_evidence": {
            "conveyed": True,
            "validated": True,
            "output_shape": "markdown",
            "contract_sha256": runner.canonical_deliverable_contract_sha256(resolved),
            "channel": "messages[0].content",
        },
        "budget_evidence": {
            "requested_timeout_seconds": 30,
            "task_class_timeout_ceiling_seconds": 60,
            "execution_timeout_seconds": 30,
            "terminal_delivery_margin_seconds": 5,
        },
    }
    if cleaned_alias is not None:
        # A non-authoritative field: the runner must judge `response`, never a
        # separately cleaned candidate.
        terminal["cleaned_content"] = cleaned_alias

    monkeypatch.setattr(
        runner,
        "_resolve_live_workspace_root",
        lambda: (tmp_path, tmp_path / "onex"),
    )

    def completed_process(command: list[str], **_: object) -> CompletedProcess[str]:
        return CompletedProcess(
            args=command, returncode=0, stdout=json.dumps({"terminal": terminal})
        )

    monkeypatch.setattr(runner.subprocess, "run", completed_process)
    return runner.run_live_manifest(_manifest(), timeout_seconds=30)


@pytest.mark.parametrize(
    ("returned_content", "expected"),
    [
        (
            "Okay, let me draft the artifact.\n\n" + _ARTIFACT,
            "planning_prose_in_caller_bytes",
        ),
        (
            _ARTIFACT + "\n\nLet me know if you want changes.",
            "trailing_self_review_in_caller_bytes",
        ),
    ],
    ids=("planning-prose", "trailing-self-review"),
)
def test_runner_reports_pollution_in_the_returned_bytes_beside_a_clean_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    returned_content: str,
    expected: str,
) -> None:
    receipt = _run_terminal_content(
        monkeypatch, tmp_path, returned_content, cleaned_alias=_ARTIFACT
    )
    trial = receipt["contracts"][0]["trials"][0]  # type: ignore[index]
    assert trial["returned_content_valid"] is True, "the lax pattern alone passes it"
    assert expected in trial["output_only"]["refusals"]
    assert trial["passed"] is False
    assert receipt["passed"] is False


def test_runner_passes_a_clean_markdown_terminal_on_the_runtime_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Positive control: nothing cut, clean bytes, every other check true."""
    receipt = _run_terminal_content(monkeypatch, tmp_path, _ARTIFACT)
    trial = receipt["contracts"][0]["trials"][0]  # type: ignore[index]
    assert trial["output_only"]["accepted"] is True
    assert trial["output_only"]["evidence_basis"] == "runtime_extraction_count"
    assert trial["passed"] is True
    assert receipt["passed"] is True


def test_runner_refuses_a_terminal_whose_runtime_count_shows_a_cut(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    receipt = _run_terminal_content(
        monkeypatch, tmp_path, _ARTIFACT, preamble_chars=len("Here it is:\n")
    )
    trial = receipt["contracts"][0]["trials"][0]  # type: ignore[index]
    assert trial["output_only"]["refusals"] == ["extraction_required_leading_text"]
    assert trial["passed"] is False


# ---------------------------------------------------------------------------
# The runtime-count evidence basis, directly.
# ---------------------------------------------------------------------------


def test_runtime_count_accepts_nothing_cut_or_exactly_the_marker_line() -> None:
    marker_line = len("### ANSWER") + 1
    for count in (0, marker_line):
        verdict = evaluate_output_only(
            raw_response=None,
            caller_bytes=_ARTIFACT,
            contract=_MARKDOWN,
            runtime_leading_chars=count,
        )
        assert verdict.accepted is True, count
        assert (
            verdict.evidence_basis
            is EnumOutputOnlyEvidenceBasis.RUNTIME_EXTRACTION_COUNT
        )
    refused = evaluate_output_only(
        raw_response=None,
        caller_bytes=_ARTIFACT,
        contract=_MARKDOWN,
        runtime_leading_chars=marker_line + 1,
    )
    assert refused.refusals == (EnumOutputOnlyRefusal.EXTRACTION_REQUIRED_LEADING_TEXT,)


def test_runtime_count_cannot_prove_a_json_deliverable_had_nothing_after_it() -> None:
    contract = resolve_deliverable_contract({"type": "object"})
    verdict = evaluate_output_only(
        raw_response=None,
        caller_bytes="{}",
        contract=contract,
        runtime_leading_chars=0,
    )
    assert verdict.refusals == (EnumOutputOnlyRefusal.EXTRACTION_EVIDENCE_INCOMPLETE,)
    raw_verdict = evaluate_output_only(
        raw_response="{}", caller_bytes="{}", contract=contract
    )
    assert raw_verdict.accepted is True
    assert raw_verdict.evidence_basis is EnumOutputOnlyEvidenceBasis.RAW_PROVIDER_BYTES


def test_no_raw_bytes_and_no_count_is_refused() -> None:
    verdict = _verdict(None, _ARTIFACT)
    assert verdict.refusals == (EnumOutputOnlyRefusal.RAW_PROVIDER_BYTES_ABSENT,)
    assert verdict.evidence_basis is EnumOutputOnlyEvidenceBasis.ABSENT
