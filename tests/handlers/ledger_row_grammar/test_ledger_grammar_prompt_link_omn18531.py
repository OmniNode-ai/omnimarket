# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# Dropped no source tests or cases; all exercise grammar functions.
"""Prompt links, confirmations, reviewed ranges and correction spans."""

from __future__ import annotations

import pytest

from omnimarket.handlers import ledger_row_grammar as g

pytestmark = pytest.mark.unit
LEDGER = "docs/tracking/ROLLING_WORK_LEDGER.md"
PROMPT = "6f1c2d3e-4a5b-4c6d-8e7f-0a1b2c3d4e5f"
PROMPT_B = "7a1c2d3e-4a5b-4c6d-8e7f-0a1b2c3d4e5f"
CONFIRMER = "8b1c2d3e-4a5b-4c6d-8e7f-0a1b2c3d4e5f"
DIGEST = "a" * 64
WORDS = "ship the compaction mod first"


def _ruling(*cells: str) -> str:
    extra = "".join(f" | {cell}" for cell in cells)
    return (
        "2026-10-15T06:00:00Z | RULING | lane=lane-a | ticket=OMN-18531 | "
        f'question=what first?{extra} | "{WORDS}"'
    )


def _consent(*cells: str) -> str:
    extra = "".join(f" | {cell}" for cell in cells)
    return (
        "2026-10-15T06:00:00Z | OPERATOR-CONSENT | lane=lane-a | "
        f'ticket=OMN-18531{extra} | "{WORDS}" | APPROVED SCOPE: a | OUT OF SCOPE: b'
    )


def _correction(*cells: str) -> str:
    extra = "".join(f" | {cell}" for cell in cells)
    return f"2026-10-15T06:05:00Z | CORRECTION | lane=lane-a{extra} | the framing was wrong"


def test_control_unlinked_ruling_still_conforms() -> None:
    assert g.row_refusals(_ruling()) == []


def test_ruling_with_prompt_id_and_covers_conforms() -> None:
    assert g.row_refusals(_ruling(f"prompt_id={PROMPT}", "covers=12:41")) == []


def test_consent_with_prompt_id_and_covers_conforms() -> None:
    assert g.row_refusals(_consent(f"prompt_id={PROMPT}", "covers=0:29")) == []


@pytest.mark.parametrize(
    "cells",
    [
        ("prompt_id=not-a-uuid", "covers=12:41"),
        (f"prompt_id={PROMPT}-x", "covers=12:41"),
        (f"prompt_id={PROMPT.upper()}", "covers=12:41"),
        (f"prompt_id={PROMPT}", "covers=41:12"),
        (f"prompt_id={PROMPT}", "covers=12:12"),
        (f"prompt_id={PROMPT}", "covers=a:b"),
        (f"prompt_id={PROMPT}", "covers=-1:5"),
        (f"prompt_id={PROMPT}", "covers=12"),
        (f"prompt_id={PROMPT}",),
        ("covers=12:41",),
    ],
    ids=[
        "prompt-id-not-uuid",
        "prompt-id-suffix",
        "prompt-id-uppercase",
        "covers-reversed",
        "covers-empty",
        "covers-not-numbers",
        "covers-negative",
        "covers-one-bound",
        "prompt-id-without-covers",
        "covers-without-prompt-id",
    ],
)
def test_malformed_link_is_refused(cells: tuple[str, ...]) -> None:
    assert g.row_refusals(_ruling(*cells)), cells


def test_ambiguous_match_conforms_without_a_prompt_id() -> None:
    assert g.row_refusals(_ruling("prompt_match=ambiguous")) == []


@pytest.mark.parametrize(
    "cells",
    [
        ("prompt_match=maybe",),
        ("prompt_match=",),
        ("prompt_match=ambiguous", f"prompt_id={PROMPT}", "covers=1:5"),
        ("prompt_match=ambiguous", "covers=1:5"),
    ],
    ids=["unknown-value", "empty", "with-prompt-id", "with-covers"],
)
def test_prompt_match_is_only_ambiguous_and_never_beside_a_link(
    cells: tuple[str, ...],
) -> None:
    assert g.row_refusals(_ruling(*cells))


def test_confirmed_candidate_conforms() -> None:
    cells = (
        f"confirms={DIGEST}",
        f"confirmed_by={CONFIRMER}",
        f"prompt_id={PROMPT}",
        "covers=12:41",
    )
    assert g.row_refusals(_ruling(*cells)) == []


@pytest.mark.parametrize(
    "cells",
    [
        (f"confirms={DIGEST}", f"prompt_id={PROMPT}", "covers=12:41"),
        (f"confirmed_by={CONFIRMER}", f"prompt_id={PROMPT}", "covers=12:41"),
        (f"confirms={DIGEST}", f"confirmed_by={CONFIRMER}"),
        (
            "confirms=abc",
            f"confirmed_by={CONFIRMER}",
            f"prompt_id={PROMPT}",
            "covers=1:5",
        ),
        (
            f"confirms={'g' * 64}",
            f"confirmed_by={CONFIRMER}",
            f"prompt_id={PROMPT}",
            "covers=1:5",
        ),
        (
            f"confirms={DIGEST}",
            "confirmed_by=nope",
            f"prompt_id={PROMPT}",
            "covers=1:5",
        ),
    ],
    ids=[
        "no-confirmed-by",
        "no-confirms",
        "no-link",
        "short-digest",
        "non-hex-digest",
        "confirmer-not-uuid",
    ],
)
def test_malformed_confirmation_is_refused(cells: tuple[str, ...]) -> None:
    assert g.row_refusals(_ruling(*cells))


def test_reviewed_range_on_a_ruling_conforms() -> None:
    assert g.row_refusals(_ruling(f"reviewed={PROMPT}..{PROMPT_B}")) == []


def test_reviewed_range_may_also_be_linked_like_any_ruling() -> None:
    cells = (f"reviewed={PROMPT}..{PROMPT_B}", f"prompt_id={CONFIRMER}", "covers=0:10")
    assert g.row_refusals(_ruling(*cells)) == []


@pytest.mark.parametrize(
    "value",
    [
        PROMPT,
        f"{PROMPT}..",
        f"..{PROMPT_B}",
        f"{PROMPT}..not-a-uuid",
        f"not-a-uuid..{PROMPT_B}",
        f"{PROMPT}...{PROMPT_B}",
        f"{PROMPT},{PROMPT_B}",
        "",
    ],
    ids=[
        "single-id",
        "missing-last",
        "missing-first",
        "last-not-uuid",
        "first-not-uuid",
        "three-dots",
        "comma",
        "empty",
    ],
)
def test_malformed_reviewed_range_is_refused(value: str) -> None:
    assert g.row_refusals(_ruling(f"reviewed={value}"))


def test_reviewed_is_ruling_only() -> None:
    assert g.row_refusals(_consent(f"reviewed={PROMPT}..{PROMPT_B}"))


def test_link_fields_are_not_allowed_on_other_row_types() -> None:
    row = f"2026-10-15T06:00:00Z | STATUS | lane=lane-a | ticket=OMN-18531 | prompt_id={PROMPT} | s"
    assert g.row_refusals(row)


def test_correction_operator_span_conforms_in_shape() -> None:
    assert (
        g.row_refusals(
            _correction("corrects=2026-10-15T06:00:00Z", "operator_span=0:4")
        )
        == []
    )


@pytest.mark.parametrize("value", ["4:0", "3:3", "x:9", "5", "-1:3", ""])
def test_malformed_operator_span_is_refused(value: str) -> None:
    assert g.row_refusals(
        _correction("corrects=2026-10-15T06:00:00Z", f"operator_span={value}")
    )


def test_operator_span_needs_corrects() -> None:
    assert g.row_refusals(
        _correction("amends=2026-10-15T06:00:00Z", "operator_span=0:4")
    )


def test_operator_span_is_correction_only() -> None:
    assert g.row_refusals(_ruling("operator_span=0:4"))


def _existing() -> str:
    return _ruling() + "\n"


def test_span_inside_the_target_quote_is_accepted() -> None:
    span = f"operator_span=0:{len(WORDS)}"
    payload = _correction("corrects=2026-10-15T06:00:00Z", span)
    assert g.refusal_for_state(payload, _existing(), LEDGER) is None


def test_span_one_past_the_target_quote_is_refused() -> None:
    span = f"operator_span=0:{len(WORDS) + 1}"
    payload = _correction("corrects=2026-10-15T06:00:00Z", span)
    reason = g.refusal_for_state(payload, _existing(), LEDGER)
    assert reason is not None
    assert "operator_span" in reason


def test_span_naming_a_target_absent_from_the_ledger_is_refused() -> None:
    payload = _correction("corrects=2026-10-01T00:00:00Z", "operator_span=0:4")
    reason = g.refusal_for_state(payload, _existing(), LEDGER)
    assert reason is not None
    assert "operator_span" in reason


def test_span_with_a_lane_suffixed_target_matches_the_lane() -> None:
    payload = _correction("corrects=2026-10-15T06:00:00Z-lane-b", "operator_span=0:4")
    assert g.refusal_for_state(payload, _existing(), LEDGER) is not None
    payload = _correction("corrects=2026-10-15T06:00:00Z-lane-a", "operator_span=0:4")
    assert g.refusal_for_state(payload, _existing(), LEDGER) is None


def test_correction_without_a_span_needs_no_target_on_the_ledger() -> None:
    payload = _correction("corrects=2026-10-01T00:00:00Z")
    assert g.refusal_for_state(payload, _existing(), LEDGER) is None


def test_span_refusal_leaves_an_ungoverned_ledger_alone() -> None:
    payload = _correction("corrects=2026-10-01T00:00:00Z", "operator_span=0:4")
    assert g.refusal_for_state(payload, _existing(), "notes.md") is None
