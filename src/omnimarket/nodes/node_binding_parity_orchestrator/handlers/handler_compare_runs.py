# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``compare_runs``: the semantic projection of every run against the baseline run."""

from __future__ import annotations

import re

from omnimarket.nodes.node_binding_parity_orchestrator.models.model_parity import (
    ModelCompareResult,
    ModelRunComparison,
    ModelScenarioRow,
    ModelScenarioRun,
)

_BINDING_TOKEN = re.compile(r"\s*\bbinding=\S+")


def _semantic_line(line: str) -> str:
    return _BINDING_TOKEN.sub("", line).strip()


def _row_difference(
    index: int,
    expected: ModelScenarioRow,
    actual: ModelScenarioRow,
    actual_keys: list[str],
) -> str:
    if expected.key == actual.key:
        return (
            f"row content at index {index} (key {expected.key!r}): "
            f"expected {expected.content!r}, got {actual.content!r}"
        )
    if expected.key in actual_keys:
        return (
            f"row order at index {index}: expected key {expected.key!r}, "
            f"got key {actual.key!r}"
        )
    return f"missing row {expected.key!r} (baseline index {index})"


def _first_difference(
    baseline: ModelScenarioRun, other: ModelScenarioRun, allowed: tuple[str, ...]
) -> str:
    """The first semantic difference in a fixed section order, or an empty string."""
    if (
        "proof_eligible" not in allowed
        and baseline.proof_eligible != other.proof_eligible
    ):
        return (
            f"proof_eligible: baseline {baseline.proof_eligible}, "
            f"got {other.proof_eligible}"
        )
    for index, expected in enumerate(baseline.verdicts):
        if index >= len(other.verdicts) or other.verdicts[index] != expected:
            got = other.verdicts[index] if index < len(other.verdicts) else "<none>"
            return f"verdict at index {index}: expected {expected!r}, got {got!r}"
    if len(other.verdicts) > len(baseline.verdicts):
        return f"extra verdict {other.verdicts[len(baseline.verdicts)]!r}"
    base_lines = [_semantic_line(line) for line in baseline.lines]
    other_lines = [_semantic_line(line) for line in other.lines]
    for index, expected_line in enumerate(base_lines):
        if index >= len(other_lines) or other_lines[index] != expected_line:
            got_line = other_lines[index] if index < len(other_lines) else "<none>"
            return (
                f"line at index {index}: expected {expected_line!r}, got {got_line!r}"
            )
    if len(other_lines) > len(base_lines):
        return f"extra line {other_lines[len(base_lines)]!r}"
    other_keys = [row.key for row in other.rows]
    for index, expected_row in enumerate(baseline.rows):
        if index >= len(other.rows):
            return f"missing row {expected_row.key!r} (baseline index {index})"
        actual_row = other.rows[index]
        if (expected_row.key, expected_row.content) != (
            actual_row.key,
            actual_row.content,
        ):
            return _row_difference(index, expected_row, actual_row, other_keys)
    if len(other.rows) > len(baseline.rows):
        return f"extra row {other.rows[len(baseline.rows)].key!r}"
    for name in sorted(set(baseline.counts) | set(other.counts)):
        if baseline.counts.get(name) != other.counts.get(name):
            return (
                f"count {name}: baseline {baseline.counts.get(name)}, "
                f"got {other.counts.get(name)}"
            )
    return ""


def _provenance_errors(run: ModelScenarioRun) -> tuple[str, ...]:
    if run.binding != run.profile_id:
        return (
            f"provenance: run of profile {run.profile_id!r} carries "
            f"binding={run.binding!r}",
        )
    return ()


def compare_runs(
    runs: tuple[ModelScenarioRun, ...], allowed_differences: tuple[str, ...] = ()
) -> ModelCompareResult:
    """Compare every run after the first (the baseline) with the baseline's projection.

    Provenance (``binding=``, host, endpoint, stamps, run ids) is validated on its own:
    ``binding`` must equal the profile that ran, and nothing else about it is compared.
    """
    if len(runs) < 2:
        raise ValueError("compare_runs needs the baseline run and at least one other")
    baseline = runs[0]
    comparisons: list[ModelRunComparison] = []
    for other in runs[1:]:
        difference = _first_difference(baseline, other, allowed_differences)
        errors = _provenance_errors(baseline) + _provenance_errors(other)
        comparisons.append(
            ModelRunComparison(
                baseline_profile=baseline.profile_id,
                other_profile=other.profile_id,
                passed=not difference and not errors,
                first_difference=(
                    f"{other.profile_id}: {difference}" if difference else ""
                ),
                provenance_errors=errors,
            )
        )
    failing = [c for c in comparisons if not c.passed]
    first = ""
    if failing:
        first = failing[0].first_difference or failing[0].provenance_errors[0]
    return ModelCompareResult(
        passed=not failing, comparisons=tuple(comparisons), first_difference=first
    )
