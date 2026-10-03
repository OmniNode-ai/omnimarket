# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Source conservation and canonical answer ordering across delegation computes.

Recombine joins answers with two newlines; it is not a raw source concatenator.
Listed migration units roundtrip exactly in that canonical representation.
Markdown and diff splits preserve source bytes, with shared diff headers retained
on each hunk as required by the splitter contract.
"""

from __future__ import annotations

import pytest

from omnimarket.inference.task_class_authority import load_task_class_authority
from omnimarket.models.delegation.model_size_band import (
    EnumSizeBand,
    ModelSizeBand,
    ModelSizeBandRefusal,
    ModelSizeBandRequest,
)
from omnimarket.models.model_delegation_split_recombine import (
    DelegationTaskClass,
    EnumDelegationSizeBand,
    ModelDelegationFinding,
    ModelDelegationRecombineRequest,
    ModelDelegationSplitRequest,
    ModelDelegationSplitSuccess,
    ModelDelegationUnitAnswer,
    ModelNotDecomposable,
)
from omnimarket.nodes.node_delegation_recombine_compute import (
    HandlerDelegationRecombine,
)
from omnimarket.nodes.node_delegation_size_band_compute import (
    HandlerDelegationSizeBand,
)
from omnimarket.nodes.node_delegation_split_compute import HandlerDelegationSplit

pytestmark = pytest.mark.unit

FILE_A = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
FILE_B = "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n"
HUNK_A = "@@ -1 +1 @@\n-old_a\n+new_a\n"
HUNK_B = "@@ -9,2 +9,2 @@\n-old_b\n+new_b\n"


def _recombine(
    request: ModelDelegationSplitRequest,
    parts: tuple[str, ...],
    split: ModelDelegationSplitSuccess | None = None,
) -> str:
    # Reverse arrival order so a concatenation in arrival order cannot pass.
    answers = tuple(
        ModelDelegationUnitAnswer(
            unit_id=split.units[position].unit_id
            if split
            else f"{request.task_id}:{position}",
            position=split.units[position].position if split else position,
            answer=part,
        )
        for position, part in enumerate(parts)
    )
    result = HandlerDelegationRecombine().handle(
        ModelDelegationRecombineRequest(
            task_id=request.task_id,
            task_class=request.task_class,
            answers=tuple(reversed(answers)),
        )
    )
    assert result.task_id == request.task_id
    assert result.task_class == request.task_class
    return result.answer


@pytest.mark.parametrize("count", [1, 2, 7])
@pytest.mark.parametrize(
    "band",
    [
        EnumDelegationSizeBand.MEDIUM,
        EnumDelegationSizeBand.LARGE,
        EnumDelegationSizeBand.EXTRA_LARGE,
    ],
)
def test_listed_units_roundtrip(count: int, band: EnumDelegationSizeBand) -> None:
    parts = tuple(f"ALTER TABLE table_{index}; -- café\r\n" for index in range(count))
    request = ModelDelegationSplitRequest(
        task_id="migration",
        task_class="migration",
        size_band=band,
        migration_units=parts,
    )
    split = HandlerDelegationSplit().handle(request)
    if count == 1:
        assert isinstance(split, ModelNotDecomposable)
        assert split.reason == "no_split_boundary"
        # Atomic work stays whole when split is refused.
        assert _recombine(request, parts) == parts[0]
    else:
        assert isinstance(split, ModelDelegationSplitSuccess)
        assert tuple(unit.source for unit in split.units) == parts
        assert tuple(unit.position for unit in split.units) == tuple(range(count))
        assert tuple(unit.unit_id for unit in split.units) == tuple(
            f"migration:{index}" for index in range(count)
        )
        assert all(unit.task_class == request.task_class for unit in split.units)
        bands = tuple(EnumDelegationSizeBand)
        assert all(
            unit.size_band == bands[bands.index(band) - 1] for unit in split.units
        )
        assert _recombine(
            request, tuple(unit.source for unit in split.units), split
        ) == ("\n\n".join(parts))


@pytest.mark.parametrize("characters", [15999, 16000, 16001, 63999, 64000, 64001])
def test_boundary_sized_sources_roundtrip(characters: int) -> None:
    parts = ("a" * (characters // 2), "β" * (characters - characters // 2))
    request = ModelDelegationSplitRequest(
        task_id="boundary",
        task_class="migration",
        size_band="large",
        migration_units=parts,
    )
    split = HandlerDelegationSplit().handle(request)
    assert isinstance(split, ModelDelegationSplitSuccess)
    assert tuple(unit.source for unit in split.units) == parts
    assert _recombine(request, tuple(unit.source for unit in split.units), split) == (
        "\n\n".join(parts)
    )


@pytest.mark.parametrize(
    ("count", "characters"),
    [(1, 64), (2, 16000), (7, 16001), (2, 64000), (7, 64001)],
)
def test_markdown_roundtrip_with_canonical_answer_separators(
    count: int, characters: int
) -> None:
    source = "\n\n".join(f"# Section {index}\nbody {index}" for index in range(count))
    source += "x" * (characters - len(source))
    assert len(source) == characters
    request = ModelDelegationSplitRequest(
        task_id="canonical", task_class="document", size_band="large", source=source
    )
    split = HandlerDelegationSplit().handle(request)
    if count == 1:
        assert isinstance(split, ModelNotDecomposable)
        assert split.reason == "no_split_boundary"
        assert _recombine(request, (source,)) == source
    else:
        assert isinstance(split, ModelDelegationSplitSuccess)
        assert len(split.units) == count
        assert "".join(unit.source for unit in split.units) == source
        assert all(unit.source.endswith("\n\n") for unit in split.units[:-1])
        # The recombiner owns inter-answer separators. Unit answers omit only
        # those separators, retaining all other source characters verbatim.
        answers = tuple(unit.source.removesuffix("\n\n") for unit in split.units[:-1])
        answers += (split.units[-1].source,)
        assert _recombine(request, answers, split) == source


@pytest.mark.parametrize("task_class", ["summarization", "document"])
@pytest.mark.parametrize(
    "parts",
    [
        ("# A\nalpha\n", "# B\nbeta"),
        ("preamble\r\n", "# A\r\ncafé\r\n", "   ## B\r\nβ\r\n"),
        (
            "# A\n````python\n# hidden\n~~~\n```\n```` trailing\n"
            "# still hidden\n`````\n",
            "# B\n~~~text\n## hidden\n~~~\n",
            "#\nend\n",
        ),
    ],
)
def test_markdown_preserves_source_and_canonical_answer_order(
    task_class: DelegationTaskClass, parts: tuple[str, ...]
) -> None:
    source = "".join(parts)
    request = ModelDelegationSplitRequest(
        task_id="sections", task_class=task_class, size_band="large", source=source
    )
    split = HandlerDelegationSplit().handle(request)
    assert isinstance(split, ModelDelegationSplitSuccess)
    actual = tuple(unit.source for unit in split.units)
    assert actual == parts
    assert "".join(actual) == source
    assert _recombine(request, actual, split) == "\n\n".join(parts)


@pytest.mark.parametrize("task_class", ["review", "code_review"])
@pytest.mark.parametrize(
    ("source", "parts"),
    [
        (
            "preamble\n" + FILE_A + HUNK_A + FILE_B + HUNK_B,
            ("preamble\n" + FILE_A + HUNK_A, FILE_B + HUNK_B),
        ),
        (
            "--- a/a.py\n+++ b/a.py\n" + HUNK_A + "--- a/b.py\n+++ b/b.py\n" + HUNK_B,
            ("--- a/a.py\n+++ b/a.py\n" + HUNK_A, "--- a/b.py\n+++ b/b.py\n" + HUNK_B),
        ),
        (FILE_A + HUNK_A + HUNK_B, (FILE_A + HUNK_A, FILE_A + HUNK_B)),
    ],
)
def test_review_recombines_file_and_hunk_answers_in_source_order(
    task_class: DelegationTaskClass, source: str, parts: tuple[str, ...]
) -> None:
    request = ModelDelegationSplitRequest(
        task_id="diff", task_class=task_class, size_band="large", source=source
    )
    split = HandlerDelegationSplit().handle(request)
    assert isinstance(split, ModelDelegationSplitSuccess)
    assert tuple(unit.source for unit in split.units) == parts
    assert _recombine(request, tuple(unit.source for unit in split.units), split) == (
        "\n\n".join(parts)
    )
    if source == FILE_A + HUNK_A + HUNK_B:
        assert parts == (FILE_A + HUNK_A, FILE_A + HUNK_B)
    else:
        assert "".join(parts) == source


@pytest.mark.parametrize(
    "task_class", ["summarization", "document", "review", "code_review", "migration"]
)
def test_empty_input_is_refused(task_class: DelegationTaskClass) -> None:
    result = HandlerDelegationSplit().handle(
        ModelDelegationSplitRequest(
            task_id="empty", task_class=task_class, size_band="large"
        )
    )
    assert isinstance(result, ModelNotDecomposable)
    assert result.reason == "no_split_boundary"
    assert result.task_id == "empty"
    assert result.task_class == task_class


@pytest.mark.parametrize("source", ["plain text", FILE_A, FILE_A + HUNK_A])
def test_atomic_review_is_refused_and_answer_stays_whole(source: str) -> None:
    request = ModelDelegationSplitRequest(
        task_id="atomic", task_class="review", size_band="large", source=source
    )
    split = HandlerDelegationSplit().handle(request)
    assert isinstance(split, ModelNotDecomposable)
    assert split.reason == "no_split_boundary"
    assert _recombine(request, (source,)) == source


@pytest.mark.parametrize("task_class", ["planning", "reasoning", "code_generation"])
def test_unsupported_task_classes_are_refused(task_class: DelegationTaskClass) -> None:
    result = HandlerDelegationSplit().handle(
        ModelDelegationSplitRequest(
            task_id="opaque",
            task_class=task_class,
            size_band="large",
            source="# A\n# B\n",
        )
    )
    assert isinstance(result, ModelNotDecomposable)
    assert result.reason == "task_class_not_decomposable"


def test_minimum_band_refuses_even_with_split_boundaries() -> None:
    result = HandlerDelegationSplit().handle(
        ModelDelegationSplitRequest(
            task_id="small",
            task_class="document",
            size_band="small",
            source="# A\n# B\n",
        )
    )
    assert isinstance(result, ModelNotDecomposable)
    assert result.reason == "minimum_size_band"


@pytest.mark.parametrize(
    ("feature", "threshold_key"),
    [("input_tokens", "tokens"), ("units", "units"), ("steps", "steps")],
)
@pytest.mark.parametrize("edge", ["s_max", "m_max"])
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_each_size_threshold_is_inclusive(
    feature: str, threshold_key: str, edge: str, offset: int
) -> None:
    authority = load_task_class_authority()
    thresholds = authority.size_band_thresholds
    assert thresholds is not None
    edges = getattr(thresholds, threshold_key)
    value = getattr(edges, edge) + offset
    request = ModelSizeBandRequest(
        task_class="summarization",
        prompt="x" * (value * 4) if feature == "input_tokens" else "",
        context_pack="".join(f"# Section {index}\n" for index in range(value))
        if feature == "units"
        else "",
        acceptance_criteria=tuple(f"criterion {index}" for index in range(value))
        if feature == "steps"
        else (),
    )
    measured = HandlerDelegationSizeBand(authority).handle(request)
    assert isinstance(measured, ModelSizeBand)
    expected = (
        EnumSizeBand.S
        if edge == "s_max" and offset <= 0
        else EnumSizeBand.L
        if edge == "m_max" and offset > 0
        else EnumSizeBand.M
    )
    actual = getattr(measured, feature)
    assert actual.value == value
    assert actual.band is expected
    assert measured.band is expected
    assert actual.threshold.rule == (
        f"S up to {edges.s_max}, M up to {edges.m_max}, L above"
    )


@pytest.mark.parametrize(
    ("characters", "expected"),
    [
        (16000, EnumSizeBand.S),
        (16001, EnumSizeBand.M),
        (64000, EnumSizeBand.M),
        (64001, EnumSizeBand.L),
    ],
)
def test_character_rounding_crosses_token_thresholds(
    characters: int, expected: EnumSizeBand
) -> None:
    result = HandlerDelegationSizeBand().handle(
        ModelSizeBandRequest(task_class="document", prompt="x" * characters)
    )
    assert isinstance(result, ModelSizeBand)
    assert result.input_tokens.value == (characters + 3) // 4
    assert result.band is expected


def test_size_band_refusal_branches() -> None:
    handler = HandlerDelegationSizeBand()
    supplied = handler.handle(
        ModelSizeBandRequest(task_class="unknown", prompt="", band=None)
    )
    assert isinstance(supplied, ModelSizeBandRefusal)
    assert supplied.reason == "size_input_supplied"
    assert supplied.fields == ("band",)
    unknown = handler.handle(ModelSizeBandRequest(task_class="unknown", prompt=""))
    assert isinstance(unknown, ModelSizeBandRefusal)
    assert unknown.reason == "task_class_unavailable"
    assert unknown.fields == ("task_class",)
    authority = load_task_class_authority().model_copy(
        update={"size_band_thresholds": None}
    )
    unavailable = HandlerDelegationSizeBand(authority).handle(
        ModelSizeBandRequest(task_class="document", prompt="")
    )
    assert isinstance(unavailable, ModelSizeBandRefusal)
    assert unavailable.reason == "size_thresholds_unavailable"
    assert unavailable.fields == ("size_band_thresholds",)


def test_recombine_canonicalizes_duplicate_findings() -> None:
    first = ModelDelegationFinding(path="a.py", line=1, message="Alpha")
    other = ModelDelegationFinding(path="b.py", line=1, message="Other")
    result = HandlerDelegationRecombine().handle(
        ModelDelegationRecombineRequest(
            task_id="findings",
            task_class="review",
            answers=(
                ModelDelegationUnitAnswer(
                    unit_id="second",
                    position=1,
                    answer="second",
                    findings=(
                        ModelDelegationFinding(path="a.py", line=1, message="Zulu"),
                        other,
                    ),
                ),
                ModelDelegationUnitAnswer(
                    unit_id="first", position=0, answer="first", findings=(first,)
                ),
            ),
        )
    )
    assert result.answer == "first\n\nsecond"
    assert result.findings == (first, other)
