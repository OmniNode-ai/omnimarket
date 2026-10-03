# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure size measurement from text and supplied contract thresholds."""

from __future__ import annotations

import re
from collections.abc import Iterator

from omnimarket.inference.task_class_authority import (
    ModelBandEdges,
    ModelSizeBandThresholds,
)
from omnimarket.models.delegation.model_feature_provenance import ModelFeatureProvenance
from omnimarket.models.delegation.model_size_band import (
    EnumSizeBand,
    ModelSizeBand,
    ModelSizeFeature,
)


def _lines(text: str) -> Iterator[tuple[str, bool]]:
    """Mark lines outside matching backtick or tilde fences."""
    fence_char = ""
    fence_length = 0
    for line in text.splitlines():
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            marker, suffix = fence.groups()
            if not fence_char:
                fence_char, fence_length = marker[0], len(marker)
            elif (
                marker[0] == fence_char
                and len(marker) >= fence_length
                and not suffix.strip()
            ):
                fence_char = ""
            yield line, False
        else:
            yield line, not fence_char


def _structure(text: str, *, count_items: bool) -> tuple[int, set[str]]:
    """Count structural units and distinct prompt deliverables."""
    units = 0
    deliverables: set[str] = set()
    in_rows = False
    for line, outside_fence in _lines(text):
        if line.startswith("diff --git "):
            units += 1
        if not outside_fence:
            in_rows = False
            continue
        if re.match(r"^ {0,3}#{1,6}(\s|$)", line):
            units += 1
        row = line.lstrip().startswith("|")
        if row and not in_rows:
            units += 1
        in_rows = row
        if count_items:
            item = re.match(r"^\s{0,3}(?:\d+[.)]|[-*+])\s+(\S.*)$", line)
            if item:
                units += 1
                deliverables.add(item.group(1).strip().casefold())
    return units, deliverables


def _feature(
    *,
    name: str,
    threshold_key: str,
    value: int,
    edges: ModelBandEdges,
    thresholds_reference: str,
    rule: str,
) -> ModelSizeFeature:
    if value <= edges.s_max:
        band = EnumSizeBand.S
    elif value <= edges.m_max:
        band = EnumSizeBand.M
    else:
        band = EnumSizeBand.L
    return ModelSizeFeature(
        value=value,
        band=band,
        source=ModelFeatureProvenance(
            source="text_measurement",
            reference=f"omnimarket.handlers.handler_size_band_measurement#{name}",
            rule=rule,
        ),
        threshold=ModelFeatureProvenance(
            source="contract",
            reference=(
                f"task_class_contracts.v1.yaml#{thresholds_reference}.{threshold_key}"
            ),
            rule=f"S up to {edges.s_max}, M up to {edges.m_max}, L above",
        ),
    )


def measure_size_band(
    *,
    task_class: str,
    prompt: str,
    context_pack: str,
    sources: tuple[str, ...],
    acceptance_criteria: tuple[str, ...],
    thresholds: ModelSizeBandThresholds,
    thresholds_reference: str,
) -> ModelSizeBand:
    """Measure each feature and return the largest contract-derived band."""
    texts = (prompt, context_pack, *sources)
    characters = sum(len(text) for text in texts)
    units, deliverables = _structure(prompt, count_items=True)
    for text in (context_pack, *sources):
        measured_units, _ = _structure(text, count_items=False)
        units += measured_units
    criteria = {item.strip().casefold() for item in acceptance_criteria if item.strip()}
    input_tokens = _feature(
        name="input_tokens",
        threshold_key="tokens",
        value=-(-characters // 4),
        edges=thresholds.tokens,
        thresholds_reference=thresholds_reference,
        rule="Total characters of prompt, context pack, and all sources divided by four, rounded up.",
    )
    unit_feature = _feature(
        name="units",
        threshold_key="units",
        value=units,
        edges=thresholds.units,
        thresholds_reference=thresholds_reference,
        rule="Count diff file lines everywhere, headings and consecutive row blocks outside fences in all texts, and each prompt list item outside fences.",
    )
    steps = _feature(
        name="steps",
        threshold_key="steps",
        value=len(criteria) + len(deliverables),
        edges=thresholds.steps,
        thresholds_reference=thresholds_reference,
        rule="Add separately the distinct nonempty stripped, casefolded acceptance criteria and the distinct stripped, casefolded prompt list-item texts outside fences.",
    )
    return ModelSizeBand(
        task_class=task_class,
        band=max(
            input_tokens.band,
            unit_feature.band,
            steps.band,
            key=lambda band: band.rank,
        ),
        input_tokens=input_tokens,
        units=unit_feature,
        steps=steps,
    )
