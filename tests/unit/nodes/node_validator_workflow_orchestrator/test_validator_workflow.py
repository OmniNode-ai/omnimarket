# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 and AC2 through the workflow (OMN-23078), on both backends.

A planted change runs through the read effect, the canonical validators chosen by contract
(Canonical File Shape first), the verdict reducer and the emit and gate effects; the reducer's
verdict matches the planted outcome and the gate posts the same conclusion.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_validator_verdict_reducer.overlay import (
    example_overlay as reducer_example,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.adapters import two_backend
from omnimarket.nodes.node_validator_workflow_orchestrator.adapters.change_readers import (
    InMemoryChangeReader,
    ModelPlantedChange,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.adapters.effects import (
    PackageContractSource,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.handlers.handler_validator_workflow import (
    load_topics,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.models.model_validator_workflow import (
    EnumGateConclusion,
    EnumSelection,
    ModelValidatorWorkflowRequest,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.overlay import (
    ModelValidatorEntry,
    example_overlay,
)

pytestmark = pytest.mark.unit

BACKENDS = pytest.mark.parametrize(
    "factory",
    [two_backend.make_in_memory_backend, two_backend.make_real_backend],
    ids=["in_memory", "real"],
)
BASELINE = two_backend.BASELINE


def baseline_change(*entries: str) -> ModelPlantedChange:
    """A change that leaves the baseline file as it is, on a base that lists ``entries``."""
    base = {"README.md": "base\n", BASELINE: "".join(f"{e}\n" for e in entries)}
    base.update(dict.fromkeys(entries, "x = 1\n"))
    return ModelPlantedChange(
        base_files=base, head_files={**base, "docs/n.md": "note\n"}
    )


@BACKENDS
def test_workflow_planted_change_verdict_matches_on_both_backends(
    factory: Callable[[Path], two_backend.Runner], tmp_path: Path
) -> None:
    run = factory(tmp_path)
    clean = run(two_backend.planted_clean(), 1)
    bad = run(two_backend.planted_noncanonical(), 2)
    assert clean.verdict is not None
    assert clean.verdict.verdict.value == "pass"
    assert bad.verdict is not None
    assert bad.verdict.verdict.value == "fail"
    assert [
        c.validator_id for c in bad.choices if c.selection is EnumSelection.SELECTED
    ] == ["canonical_file_shape"]
    assert bad.outcomes[0].rule_ids == ("noncanonical-file",)
    assert bad.verdict.failing_validators == ("canonical_file_shape",)
    assert clean.gate is not None
    assert clean.gate.conclusion is EnumGateConclusion.SUCCESS
    assert bad.gate is not None
    assert bad.gate.conclusion is EnumGateConclusion.FAILURE
    assert bad.canonical_validator_count == 1


@BACKENDS
def test_workflow_planted_ratchet_growth_is_refused_on_both_backends(
    factory: Callable[[Path], two_backend.Runner], tmp_path: Path
) -> None:
    run = factory(tmp_path)
    seeded = run(baseline_change("legacy/a.py"), 1)
    grown = run(baseline_change("legacy/a.py", "legacy/b.py"), 2)
    assert seeded.verdict is not None
    assert seeded.verdict.verdict.value == "pass"
    assert [(m.before, m.after) for m in seeded.moves] == [(None, 1)]
    assert grown.outcomes[0].failing_count == 0
    assert grown.verdict is not None
    assert grown.verdict.verdict.value == "fail"
    assert [(r.baseline, r.proposed) for r in grown.verdict.refused_ratchets] == [
        (1, 2)
    ]
    assert grown.gate is not None
    assert grown.gate.conclusion is EnumGateConclusion.FAILURE


def test_workflow_backends_agree_on_the_scenario(tmp_path: Path) -> None:
    memory = two_backend.scenario_planted_change(
        two_backend.make_in_memory_backend, tmp_path / "m"
    )
    real = two_backend.scenario_planted_change(
        two_backend.make_real_backend, tmp_path / "r"
    )
    assert (
        memory
        == real
        == {"clean": "pass", "noncanonical": "fail", "rules": ["noncanonical-file"]}
    )


def _built(
    planted: dict[str, ModelPlantedChange], **kwargs: Any
) -> two_backend.ValidatorWorkflow:
    overlay = kwargs.pop("overlay", example_overlay())
    return two_backend.build_workflow(
        overlay, reducer_example(), {}, planted=planted, **kwargs
    )


def _request(pr: int = 1) -> ModelValidatorWorkflowRequest:
    return ModelValidatorWorkflowRequest(
        correlation_id="c-1", repo=two_backend.REPO, pr=pr, head="h", base="b"
    )


def test_workflow_golden_chain_events() -> None:
    topics = load_topics()
    built = _built({f"{two_backend.REPO}#1": two_backend.planted_clean()})
    built.handler.handle(_request())
    assert [e.topic for e in built.emitter.events] == [
        topics["workflow_started"],
        topics["validator_completed"],
        topics["verdict_folded"],
        topics["workflow_completed"],
    ]
    assert all(e.payload["correlation_id"] == "c-1" for e in built.emitter.events)
    assert built.emitter.events[-1].payload["verdict"] == "pass"
    assert (
        built.emitter.events[-1].topic
        == "onex.evt.omnimarket.validator-workflow-completed.v1"
    )


def test_workflow_error_chain_events_an_unreadable_change_fails_the_gate() -> None:
    topics = load_topics()
    built = _built({})
    result = built.handler.handle(_request())
    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.startswith("read-failed")
    assert [e.topic for e in built.emitter.events] == [
        topics["workflow_started"],
        topics["workflow_failed"],
    ]
    assert [d.conclusion for d in built.gate.decisions] == [EnumGateConclusion.FAILURE]


class _EffectTypedContracts(PackageContractSource):
    """Injected break: the validator's node contract says effect, not compute."""

    def node_contract(self, node_package: str) -> dict[str, Any]:
        return {**super().node_contract(node_package), "node_type": "effect"}


def test_workflow_injected_break_a_non_canonical_validator_is_not_run() -> None:
    built = _built(
        {f"{two_backend.REPO}#1": two_backend.planted_noncanonical()},
        contracts=_EffectTypedContracts(),
    )
    result = built.handler.handle(_request())
    assert [c.selection for c in result.choices] == [EnumSelection.NOT_CANONICAL]
    assert result.outcomes == ()
    assert result.canonical_validator_count == 0


class _ExcludeEverything(PackageContractSource):
    def validation_contract(self, package: str, file_name: str) -> dict[str, Any]:
        document = super().validation_contract(package, file_name)
        return {
            **document,
            "validation": {**document["validation"], "exclude_patterns": ["**/*"]},
        }


def test_workflow_a_validator_whose_contract_excludes_the_change_is_not_applicable() -> (
    None
):
    built = _built(
        {f"{two_backend.REPO}#1": two_backend.planted_noncanonical()},
        contracts=_ExcludeEverything(),
    )
    result = built.handler.handle(_request())
    assert [c.selection for c in result.choices] == [EnumSelection.NOT_APPLICABLE]
    assert result.verdict is not None
    assert result.verdict.verdict.value == "pass"


def test_workflow_injected_break_an_unreadable_validator_contract_fails_the_verdict() -> (
    None
):
    overlay = example_overlay()
    broken = overlay.model_copy(
        update={
            "validators": (
                ModelValidatorEntry(
                    **{
                        **overlay.validators[0].model_dump(),
                        "validation_contract_file": "absent.yaml",
                    }
                ),
            )
        }
    )
    built = _built(
        {f"{two_backend.REPO}#1": two_backend.planted_clean()}, overlay=broken
    )
    result = built.handler.handle(_request())
    assert [c.selection for c in result.choices] == [EnumSelection.CONTRACT_UNREADABLE]
    assert result.verdict is not None
    assert result.verdict.verdict.value == "fail"
    assert result.gate is not None
    assert result.gate.conclusion is EnumGateConclusion.FAILURE


def test_workflow_injected_break_an_unsupported_input_kind_is_an_errored_validator() -> (
    None
):
    overlay = example_overlay()
    broken = overlay.model_copy(
        update={
            "validators": (
                ModelValidatorEntry(
                    **{**overlay.validators[0].model_dump(), "input_kind": "none"}
                ),
            )
        }
    )
    reader = InMemoryChangeReader(
        {f"{two_backend.REPO}#1": two_backend.planted_clean()}, overlay.ratchet_sources
    )
    built = _built({}, overlay=broken, reader=reader)
    result = built.handler.handle(_request())
    assert result.outcomes[0].error is not None
    assert result.verdict is not None
    assert result.verdict.errored_validators == ("canonical_file_shape",)


def test_workflow_the_first_validator_is_canonical_file_shape_by_contract() -> None:
    overlay = example_overlay()
    assert overlay.validators[0].validator_id == "canonical_file_shape"
    node = PackageContractSource().node_contract(overlay.validators[0].node_package)
    assert node["node_type"] == "compute"
    assert "validator_node" in node["tags"]
