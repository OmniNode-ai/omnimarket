# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Declared-shape extraction prevents reasoning preambles reaching customers."""

from __future__ import annotations

import pytest
from omnibase_core.models.delegation.wire import EnumDelegationOutputShape

from omnimarket.delegation.deliverable_extraction import (
    EnumDeliverableBoundaryMode,
    EnumDeliverableExtractionRefusal,
    ModelDeliverableContract,
    ModelPlainTextOutputConstraints,
    canonical_deliverable_contract_sha256,
    extract_deliverable,
    resolve_task_class_deliverable_contract,
)


def test_json_extractor_returns_only_the_last_schema_conforming_span() -> None:
    raw = 'scratchpad draft {"category":"wrong"}\nfinal {"category":"ready"}'
    contract = ModelDeliverableContract(
        output_shape=EnumDelegationOutputShape.JSON,
        min_deliverable_share=0.1,
        json_schema={
            "type": "object",
            "required": ["category"],
            "properties": {"category": {"const": "ready"}},
        },
    )

    extracted = extract_deliverable(raw, contract)

    assert extracted.deliverable == '{"category":"ready"}'
    assert extracted.preamble_chars == raw.index('{"category":"ready"}')
    assert extracted.deliverable_start == extracted.preamble_chars
    assert extracted.refusal is None


def test_json_extractor_supports_a_declared_scalar_schema() -> None:
    raw = 'reasoning 17 then final "delivered"'
    contract = ModelDeliverableContract(
        output_shape=EnumDelegationOutputShape.JSON,
        min_deliverable_share=0.1,
        json_schema={"type": "string", "const": "delivered"},
    )

    extracted = extract_deliverable(raw, contract)

    assert extracted.deliverable == '"delivered"'
    assert extracted.refusal is None


def test_json_extractor_keeps_object_schema_reasons_after_numeric_field() -> None:
    contract = ModelDeliverableContract(
        output_shape=EnumDelegationOutputShape.JSON,
        min_deliverable_share=0.1,
        json_schema={
            "type": "object",
            "required": ["verdict", "confidence"],
            "properties": {
                "verdict": {"type": "string"},
                "confidence": {"type": "number"},
            },
        },
    )

    extracted = extract_deliverable(
        'preamble {"result": "pass", "score": 0.91}', contract
    )

    assert extracted.deliverable == ""
    assert (
        extracted.refusal is EnumDeliverableExtractionRefusal.NO_SCHEMA_CONFORMING_JSON
    )
    assert any("verdict" in reason for reason in extracted.contract_failure_reasons)
    assert any("confidence" in reason for reason in extracted.contract_failure_reasons)


def test_markdown_contract_refuses_unmarked_ambiguous_text() -> None:
    contract = ModelDeliverableContract(
        output_shape=EnumDelegationOutputShape.MARKDOWN,
        min_deliverable_share=0.1,
        markers=("=== ANSWER ===",),
    )

    extracted = extract_deliverable("reasoning that looks like an answer", contract)

    assert extracted.deliverable == ""
    assert extracted.refusal is EnumDeliverableExtractionRefusal.AMBIGUOUS_UNMARKED
    assert extracted.preamble_chars == len("reasoning that looks like an answer")


def test_ninety_percent_preamble_fails_declared_share_floor() -> None:
    raw = f"{'x' * 90}\n=== ANSWER ===\n{'answer' * 2}"
    contract = ModelDeliverableContract(
        output_shape=EnumDelegationOutputShape.PLAIN_TEXT,
        min_deliverable_share=0.5,
        markers=("=== ANSWER ===",),
    )

    extracted = extract_deliverable(raw, contract)

    assert extracted.deliverable == "answeranswer"
    assert extracted.refusal is EnumDeliverableExtractionRefusal.BELOW_SHARE_FLOOR
    assert extracted.preamble_chars > len(extracted.deliverable)


def test_clean_marked_plain_text_is_accepted_without_exposing_marker() -> None:
    contract = ModelDeliverableContract(
        output_shape=EnumDelegationOutputShape.PLAIN_TEXT,
        min_deliverable_share=0.5,
        markers=("FINAL:",),
    )

    extracted = extract_deliverable("FINAL:\nship it", contract)

    assert extracted.deliverable == "ship it"
    assert extracted.refusal is None
    assert extracted.preamble_chars == len("FINAL:\n")


@pytest.mark.parametrize(
    ("raw", "expected_preamble_chars"),
    [
        ("### ANSWER\n# Delivered\n", len("### ANSWER\n")),
        (
            "scratch\n### ANSWER\n\n# Delivered\n",
            len("scratch\n### ANSWER\n\n"),
        ),
    ],
)
def test_markdown_deliverable_excludes_its_declared_boundary_marker(
    raw: str, expected_preamble_chars: int
) -> None:
    contract = ModelDeliverableContract(
        output_shape=EnumDelegationOutputShape.MARKDOWN,
        min_deliverable_share=0.1,
        markers=("### ANSWER",),
        render_start_marker="### ANSWER",
    )

    extracted = extract_deliverable(raw, contract)

    assert extracted.deliverable == "# Delivered\n"
    assert extracted.preamble_chars == expected_preamble_chars


def test_markdown_keeps_a_declared_heading_boundary() -> None:
    raw = "scratch\n## Checkpoint 2026-09-19T12:40Z\n\n# Delivered\n"
    contract = ModelDeliverableContract(
        output_shape=EnumDelegationOutputShape.MARKDOWN,
        min_deliverable_share=0.1,
        markers=("## Checkpoint 2026-09-19T12:40Z",),
    )

    extracted = extract_deliverable(raw, contract)

    assert extracted.deliverable == "## Checkpoint 2026-09-19T12:40Z\n\n# Delivered\n"
    assert extracted.preamble_chars == len("scratch\n")


def test_task_class_default_contract_has_one_render_marker_and_all_boundaries() -> None:
    contract = resolve_task_class_deliverable_contract("summarization", None)

    assert contract.output_shape is EnumDelegationOutputShape.MARKDOWN
    assert contract.render_start_marker == "### ANSWER"
    assert contract.markers == ("### ANSWER", "=== ANSWER ===")
    assert canonical_deliverable_contract_sha256(contract) == (
        "eec0c6cf299fdcb0d413c031fd0676b376d343cdbb4c2e12e93971f8a8ef8084"
    )


@pytest.mark.parametrize(
    ("raw", "markers"),
    [
        pytest.param(
            "draft analysis\n"
            "## Checkpoint draft\n"
            "discarded draft\n"
            + ("preamble " * 180)
            + "\n## Checkpoint final\n\n# Delivered\n",
            ("## Checkpoint draft", "## Checkpoint final"),
            id="last-checkpoint-marker-wins",
        ),
        pytest.param(
            "notes\n"
            "## Boundary candidate\n"
            "discarded candidate\n"
            + ("reasoning " * 180)
            + "\n## Boundary accepted\n\n# Delivered\n",
            ("## Boundary candidate", "## Boundary accepted"),
            id="last-boundary-marker-wins",
        ),
    ],
)
def test_synthetic_checkpoint_boundaries_select_the_last_declared_artifact(
    raw: str,
    markers: tuple[str, str],
) -> None:
    """Synthetic regression, not a replay or acceptance claim for historical output."""
    contract = ModelDeliverableContract(
        output_shape=EnumDelegationOutputShape.MARKDOWN,
        min_deliverable_share=0.5,
        markers=markers,
    )

    extracted = extract_deliverable(raw, contract)

    expected_start = max(raw.rfind(marker) for marker in markers)
    assert extracted.deliverable_start == expected_start
    assert extracted.preamble_chars == expected_start
    assert extracted.deliverable == raw[expected_start:]
    assert extracted.raw_chars == len(raw)
    assert extracted.refusal is EnumDeliverableExtractionRefusal.BELOW_SHARE_FLOOR

    clean = extract_deliverable(extracted.deliverable, contract)
    assert clean.deliverable == extracted.deliverable
    assert clean.preamble_chars == 0
    assert clean.raw_chars == len(clean.deliverable)
    assert clean.refusal is None


def test_synthetic_final_paragraph_boundary_preserves_raw_accounting() -> None:
    """Synthetic regression, not a replay or acceptance claim for historical output."""
    final_paragraph = " ".join(["deliverable"] * 90)
    raw = ("preamble " * 800).strip() + "\n\n" + final_paragraph
    contract = ModelDeliverableContract(
        output_shape=EnumDelegationOutputShape.PLAIN_TEXT,
        min_deliverable_share=0.5,
        boundary_mode=EnumDeliverableBoundaryMode.FINAL_PARAGRAPH,
        plain_text_constraints=ModelPlainTextOutputConstraints(
            min_words=90,
            max_words=130,
        ),
    )

    extracted = extract_deliverable(raw, contract)

    assert extracted.deliverable == raw[extracted.deliverable_start :]
    assert extracted.preamble_chars == extracted.deliverable_start
    assert extracted.raw_chars == len(raw)
    assert extracted.refusal is EnumDeliverableExtractionRefusal.BELOW_SHARE_FLOOR

    clean = extract_deliverable(extracted.deliverable, contract)
    assert clean.deliverable == extracted.deliverable
    assert clean.preamble_chars == 0
    assert clean.refusal is None
