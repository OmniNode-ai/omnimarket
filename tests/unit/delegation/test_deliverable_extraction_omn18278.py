# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Declared-shape extraction prevents reasoning preambles reaching customers."""

from __future__ import annotations

import os
from pathlib import Path

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

_RAW_REPLAY_CASES = (
    (
        "9252562e-3c0e-4556-96d6-e345f64553ca",
        ("terminal_payload", "response"),
    ),
    (
        "54a2d3f2-6bee-4a26-8a03-937da09d184e",
        ("terminal_payload", "payload", "response"),
    ),
)
_CHECKPOINT_MARKERS = (
    "## Checkpoint 2026-09-19T12:40Z",
    "## 2026-09-19T12:40Z",
)


def _replay_content(run_id: str, path: tuple[str, ...]) -> str:
    omni_home = os.environ.get("OMNI_HOME")
    if omni_home is None:
        pytest.skip("exact raw replay artifacts require OMNI_HOME")
    result_path = (
        Path(omni_home) / ".onex_state" / "runs" / run_id / "workflow_result.json"
    )
    if not result_path.is_file():
        pytest.skip(f"exact raw replay artifact is unavailable: {run_id}")
    import json

    value: object = json.loads(result_path.read_text(encoding="utf-8"))
    for key in path:
        assert isinstance(value, dict)
        value = value[key]
    assert isinstance(value, str)
    return value


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


@pytest.mark.parametrize(("run_id", "path"), _RAW_REPLAY_CASES)
def test_exact_raw_checkpoint_replays_select_the_last_declared_artifact(
    run_id: str,
    path: tuple[str, ...],
) -> None:
    raw = _replay_content(run_id, path)
    contract = ModelDeliverableContract(
        output_shape=EnumDelegationOutputShape.MARKDOWN,
        min_deliverable_share=0.5,
        markers=_CHECKPOINT_MARKERS,
    )

    extracted = extract_deliverable(raw, contract)

    expected_start = max(raw.rfind(marker) for marker in _CHECKPOINT_MARKERS)
    assert extracted.deliverable_start == expected_start
    assert extracted.preamble_chars == expected_start
    assert extracted.deliverable == raw[expected_start:]
    assert extracted.refusal is EnumDeliverableExtractionRefusal.BELOW_SHARE_FLOOR

    clean = extract_deliverable(extracted.deliverable, contract)
    assert clean.deliverable == extracted.deliverable
    assert clean.preamble_chars == 0
    assert clean.raw_chars == len(clean.deliverable)
    assert clean.refusal is None


def test_exact_final_paragraph_replay_requires_its_declared_boundary() -> None:
    raw = _replay_content(
        "0a706f7d-0b6d-40cd-9d67-4dfc9b68ed1e",
        ("terminal_payload", "response"),
    )
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
    assert extracted.raw_chars == 33412
    assert extracted.refusal is EnumDeliverableExtractionRefusal.BELOW_SHARE_FLOOR

    clean = extract_deliverable(extracted.deliverable, contract)
    assert clean.deliverable == extracted.deliverable
    assert clean.preamble_chars == 0
    assert clean.refusal is None
