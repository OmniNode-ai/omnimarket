# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compaction receipts fold: precedence, conflicts, replay and order independence."""

from __future__ import annotations

import importlib
import random
import tomllib
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from omnimarket.models.context_compaction import (
    EnumContextCompactionFailureClass as FailureClass,
)
from omnimarket.models.context_compaction import (
    EnumContextCompactionTrigger,
    ModelContextCompactionActivated,
    ModelContextCompactionFallback,
    ModelContextCompactionInstalled,
    ModelContextCompactionInstallMismatch,
    ModelContextCompactionInstallUncertain,
    ModelContextCompactionPrepared,
)
from omnimarket.models.context_compaction import (
    EnumContextInstallUncertainReason as Reason,
)
from omnimarket.models.context_compaction_fold import (
    EnumCompactionActivationState as ActivationState,
)
from omnimarket.models.context_compaction_fold import (
    EnumCompactionCandidateState as CandidateState,
)
from omnimarket.models.context_compaction_fold import (
    ModelCompactionFoldRequest,
    ModelCompactionFoldResult,
)
from omnimarket.nodes.node_context_compaction_receipts_fold_compute.handlers.handler_context_compaction_receipts_fold import (
    HandlerContextCompactionReceiptsFold,
)

pytestmark = pytest.mark.unit

SESSION = "844d62fa-0000-4000-8000-000000000001"
OTHER_SESSION = "844d62fa-0000-4000-8000-000000000002"
C1, C2, C3 = (UUID(int=i) for i in range(1, 4))
A1, A2, A3, A4, A5 = (UUID(int=i) for i in range(11, 16))
I1, I2 = UUID(int=21), UUID(int=22)
H1, H2 = "a" * 64, "b" * 64
ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = ROOT / "src/omnimarket/nodes/node_context_compaction_receipts_fold_compute"


def prepared(
    candidate_id: UUID = C1, assembly_hash: str = H1, session_id: str = SESSION
) -> ModelContextCompactionPrepared:
    return ModelContextCompactionPrepared(
        candidate_id=candidate_id,
        session_id=session_id,
        input_messages_hash=H1,
        assembly_hash=assembly_hash,
        version_vector_hash=H1,
        profile_name="default",
        profile_content_hash=H1,
    )


def activated(
    activation_id: UUID = A1,
    candidate_id: UUID = C1,
    model_outputs_hash: str = H1,
    final_pack_hash: str = H1,
    session_id: str = SESSION,
) -> ModelContextCompactionActivated:
    return ModelContextCompactionActivated(
        activation_id=activation_id,
        candidate_id=candidate_id,
        invocation_id=I1,
        session_id=session_id,
        trigger=EnumContextCompactionTrigger.MANUAL,
        model_outputs_hash=model_outputs_hash,
        final_pack_hash=final_pack_hash,
        body_hash=H1,
        installed_messages_hash=H1,
    )


def installed(
    activation_id: UUID = A1,
    boundary_uuid: str = "b1",
    session_id: str = SESSION,
) -> ModelContextCompactionInstalled:
    return ModelContextCompactionInstalled(
        activation_id=activation_id,
        session_id=session_id,
        boundary_uuid=boundary_uuid,
        observed_hash=H1,
    )


def mismatch(
    activation_id: UUID = A1, boundary_uuid: str = "b1"
) -> ModelContextCompactionInstallMismatch:
    return ModelContextCompactionInstallMismatch(
        activation_id=activation_id,
        session_id=SESSION,
        boundary_uuid=boundary_uuid,
        installed_messages_hash=H1,
        observed_hash=H2,
    )


def uncertain(
    reason: Reason,
    activation_id: UUID = A1,
    boundary_uuid: str | None = "b1",
) -> ModelContextCompactionInstallUncertain:
    return ModelContextCompactionInstallUncertain(
        activation_id=activation_id,
        session_id=SESSION,
        reason=reason,
        boundary_uuid=boundary_uuid,
    )


def fallback(
    failure_class: FailureClass = FailureClass.BUILD_ERROR,
    invocation_id: UUID = I1,
    candidate_id: UUID | None = C1,
    session_id: str = SESSION,
) -> ModelContextCompactionFallback:
    return ModelContextCompactionFallback(
        invocation_id=invocation_id,
        session_id=session_id,
        candidate_id=candidate_id,
        failure_class=failure_class,
    )


def fold(**receipts: tuple[BaseModel, ...]) -> ModelCompactionFoldResult:
    return HandlerContextCompactionReceiptsFold().handle(
        ModelCompactionFoldRequest.model_validate(receipts)
    )


def test_empty_receipts() -> None:
    assert fold() == ModelCompactionFoldResult(
        candidates=(), activations=(), fallbacks=()
    )


def test_prepared_and_activated() -> None:
    result = fold(prepared=(prepared(),), activated=(activated(),))
    [candidate], [activation] = result.candidates, result.activations
    assert candidate.state is CandidateState.PREPARED
    assert candidate.candidate_id == C1
    assert candidate.session_id == SESSION
    assert candidate.assembly_hashes == (H1,)
    assert candidate.activation_ids == (A1,)
    assert candidate.fallback_invocation_ids == ()
    assert activation.state is ActivationState.ACTIVATED
    assert activation.activated is True
    assert activation.candidate_id == C1
    assert (
        activation.reason
        is activation.boundary_uuid
        is activation.observed_hash
        is None
    )
    assert activation.conflicts == ()


def test_identical_prepared_copies_are_one_prepared_candidate() -> None:
    result = fold(prepared=(prepared(), prepared()))
    [candidate] = result.candidates
    assert candidate.state is CandidateState.PREPARED
    assert candidate.assembly_hashes == (H1,)


def test_different_assembly_hashes_conflict() -> None:
    result = fold(prepared=(prepared(assembly_hash=H2), prepared()))
    [candidate] = result.candidates
    assert candidate.state is CandidateState.CONFLICT
    assert candidate.assembly_hashes == (H1, H2)


def test_prepared_copies_differing_in_another_field_also_conflict() -> None:
    changed = prepared().model_copy(update={"profile_name": "other"})
    [candidate] = fold(prepared=(changed, prepared())).candidates
    assert candidate.state is CandidateState.CONFLICT
    assert candidate.assembly_hashes == (H1,)


def test_different_model_outputs_across_activations_are_not_a_conflict() -> None:
    result = fold(
        prepared=(prepared(),),
        activated=(
            activated(A2, model_outputs_hash=H2, final_pack_hash=H2),
            activated(),
        ),
    )
    [candidate] = result.candidates
    assert candidate.state is CandidateState.PREPARED
    assert candidate.activation_ids == (A1, A2)
    assert [a.activation_id for a in result.activations] == [A1, A2]
    assert all(a.state is ActivationState.ACTIVATED for a in result.activations)
    assert all(a.conflicts == () for a in result.activations)


def test_prepared_activated_installed() -> None:
    result = fold(
        prepared=(prepared(),), activated=(activated(),), installed=(installed(),)
    )
    [fact] = result.activations
    assert fact.state is ActivationState.INSTALLED
    assert fact.boundary_uuid == "b1"
    assert fact.observed_hash == H1
    assert fact.reason is None


@pytest.mark.parametrize("reason", list(Reason))
def test_installed_dominates_not_installed_and_uncertain(reason: Reason) -> None:
    [fact] = fold(
        installed=(installed(),), install_uncertain=(uncertain(reason),)
    ).activations
    assert fact.state is ActivationState.INSTALLED
    assert fact.boundary_uuid == "b1"
    assert fact.observed_hash == H1
    assert fact.reason is None


def test_mismatch() -> None:
    [fact] = fold(install_mismatch=(mismatch(),)).activations
    assert fact.state is ActivationState.INSTALL_MISMATCH
    assert fact.boundary_uuid == "b1"
    assert fact.observed_hash == H2


@pytest.mark.parametrize("reason", list(Reason))
def test_mismatch_dominates_not_installed_and_uncertain(reason: Reason) -> None:
    [fact] = fold(
        install_mismatch=(mismatch(),), install_uncertain=(uncertain(reason),)
    ).activations
    assert fact.state is ActivationState.INSTALL_MISMATCH
    assert fact.reason is None


@pytest.mark.parametrize("reason", [Reason.NATIVE_COMPACTION, Reason.NOT_SELECTED])
def test_not_installed(reason: Reason) -> None:
    [fact] = fold(install_uncertain=(uncertain(reason),)).activations
    assert fact.state is ActivationState.NOT_INSTALLED
    assert fact.reason is reason
    assert fact.boundary_uuid == "b1"
    assert fact.observed_hash is None


def test_not_installed_dominates_uncertain() -> None:
    [fact] = fold(
        install_uncertain=(
            uncertain(Reason.CAPTURE_GAP),
            uncertain(Reason.UNPARSEABLE_RECORD),
            uncertain(Reason.NATIVE_COMPACTION),
        )
    ).activations
    assert fact.state is ActivationState.NOT_INSTALLED
    assert fact.reason is Reason.NATIVE_COMPACTION
    assert fact.boundary_uuid == "b1"


def test_capture_gap_has_no_boundary() -> None:
    [fact] = fold(install_uncertain=(uncertain(Reason.CAPTURE_GAP),)).activations
    assert fact.state is ActivationState.UNCERTAIN
    assert fact.reason is Reason.CAPTURE_GAP
    assert fact.boundary_uuid is None


def test_unparseable_record() -> None:
    [fact] = fold(install_uncertain=(uncertain(Reason.UNPARSEABLE_RECORD),)).activations
    assert fact.state is ActivationState.UNCERTAIN
    assert fact.reason is Reason.UNPARSEABLE_RECORD
    assert fact.boundary_uuid == "b1"


def test_unparseable_record_dominates_capture_gap_and_uses_smallest_boundary() -> None:
    [fact] = fold(
        install_uncertain=(
            uncertain(Reason.UNPARSEABLE_RECORD, boundary_uuid=None),
            uncertain(Reason.UNPARSEABLE_RECORD, boundary_uuid="b2"),
            uncertain(Reason.CAPTURE_GAP, boundary_uuid=None),
            uncertain(Reason.UNPARSEABLE_RECORD, boundary_uuid="b1"),
        )
    ).activations
    assert fact.state is ActivationState.UNCERTAIN
    assert fact.reason is Reason.UNPARSEABLE_RECORD
    assert fact.boundary_uuid == "b1"
    assert fact.conflicts == ()


@pytest.mark.parametrize(
    ("receipts", "label"),
    [
        (
            {"activated": (activated(), activated(candidate_id=C2))},
            "activated_copies_differ",
        ),
        (
            {"installed": (installed(),), "install_mismatch": (mismatch(),)},
            "installed_and_mismatch",
        ),
        (
            {"installed": (installed(), installed(boundary_uuid="b2"))},
            "installed_copies_differ",
        ),
        (
            {"install_mismatch": (mismatch(), mismatch(boundary_uuid="b2"))},
            "mismatch_copies_differ",
        ),
        (
            {
                "install_uncertain": (
                    uncertain(Reason.NATIVE_COMPACTION),
                    uncertain(Reason.NOT_SELECTED),
                )
            },
            "not_installed_reasons_differ",
        ),
        (
            {
                "install_uncertain": (
                    uncertain(Reason.NATIVE_COMPACTION),
                    uncertain(Reason.NATIVE_COMPACTION, boundary_uuid="b2"),
                )
            },
            "not_installed_reasons_differ",
        ),
        (
            {
                "activated": (activated(session_id=OTHER_SESSION),),
                "installed": (installed(),),
            },
            "session_differs",
        ),
    ],
)
def test_activation_conflicts(
    receipts: dict[str, tuple[BaseModel, ...]], label: str
) -> None:
    [fact] = fold(**receipts).activations
    assert fact.state is ActivationState.CONFLICT
    assert fact.conflicts == (label,)
    assert fact.reason is fact.boundary_uuid is fact.observed_hash is None
    assert fact.session_id == SESSION


def test_installed_without_activated_event() -> None:
    result = fold(installed=(installed(),))
    [fact] = result.activations
    assert fact.state is ActivationState.INSTALLED
    assert fact.activated is False
    assert fact.candidate_id is None
    assert result.candidates == ()


def test_activated_without_prepared() -> None:
    [candidate] = fold(activated=(activated(),)).candidates
    assert candidate.state is CandidateState.UNPREPARED
    assert candidate.session_id == SESSION
    assert candidate.assembly_hashes == ()
    assert candidate.activation_ids == (A1,)


def test_fallback_without_prepared() -> None:
    [candidate] = fold(fallback=(fallback(),)).candidates
    assert candidate.state is CandidateState.UNPREPARED
    assert candidate.session_id == SESSION
    assert candidate.assembly_hashes == ()
    assert candidate.fallback_invocation_ids == (I1,)


def test_unprepared_candidate_uses_smallest_reference_session() -> None:
    [candidate] = fold(
        activated=(activated(session_id=OTHER_SESSION),), fallback=(fallback(),)
    ).candidates
    assert candidate.session_id == SESSION


def test_fallback_identical_copies_are_deduplicated() -> None:
    [fact] = fold(fallback=(fallback(), fallback())).fallbacks
    assert fact.conflict is False
    assert fact.failure_classes == (FailureClass.BUILD_ERROR,)
    assert fact.candidate_id == C1


def test_different_fallback_copies_conflict() -> None:
    [fact] = fold(
        fallback=(fallback(FailureClass.SOURCE_DISABLED), fallback())
    ).fallbacks
    assert fact.conflict is True
    assert fact.failure_classes == (
        FailureClass.BUILD_ERROR,
        FailureClass.SOURCE_DISABLED,
    )


def test_fallback_uses_smallest_non_null_candidate_and_session() -> None:
    result = fold(
        fallback=(
            fallback(candidate_id=None, session_id=OTHER_SESSION),
            fallback(candidate_id=C2),
            fallback(candidate_id=C1),
        )
    )
    [fact] = result.fallbacks
    assert fact.conflict is True
    assert fact.candidate_id == C1
    assert fact.session_id == SESSION
    assert [c.candidate_id for c in result.candidates] == [C1, C2]
    assert all(c.fallback_invocation_ids == (I1,) for c in result.candidates)
    assert fold(fallback=(fallback(candidate_id=None),)).candidates == ()


def test_orchestrator_killed_after_prepared_then_redelivered() -> None:
    result = fold(prepared=(prepared(), prepared()), activated=(activated(),))
    [candidate], [activation] = result.candidates, result.activations
    assert candidate.state is CandidateState.PREPARED
    assert candidate.activation_ids == (A1,)
    assert activation.state is ActivationState.ACTIVATED


def mixed_receipts() -> dict[str, tuple[BaseModel, ...]]:
    return {
        "prepared": (prepared(C2), prepared(), prepared(C2, H2), prepared()),
        "activated": (
            activated(A3, C3),
            activated(A2, C1, H2, H2),
            activated(),
            activated(A4, C2),
            activated(A5, C2),
            activated(A5, C3, session_id=OTHER_SESSION),
        ),
        "fallback": (
            fallback(),
            fallback(FailureClass.SOURCE_DISABLED),
            fallback(invocation_id=I2, candidate_id=C3),
            fallback(invocation_id=I2, candidate_id=None),
            fallback(),
        ),
        "installed": (installed(), installed(), installed(A5), installed(A5, "b5")),
        "install_mismatch": (mismatch(A3), mismatch(A5), mismatch(A5, "b6")),
        "install_uncertain": (
            uncertain(Reason.NATIVE_COMPACTION, A1),
            uncertain(Reason.NATIVE_COMPACTION, A2),
            uncertain(Reason.CAPTURE_GAP, A2, None),
            uncertain(Reason.UNPARSEABLE_RECORD, A4, None),
            uncertain(Reason.UNPARSEABLE_RECORD, A4, "b4"),
            uncertain(Reason.UNPARSEABLE_RECORD, A4, "b3"),
            uncertain(Reason.CAPTURE_GAP, A4),
            uncertain(Reason.NATIVE_COMPACTION, A5),
            uncertain(Reason.NOT_SELECTED, A5),
        ),
    }


def test_200_permutations_with_random_duplicate_insertion() -> None:
    receipts = mixed_receipts()
    expected = fold(**receipts).model_dump_json()
    for seed in range(200):
        rng = random.Random(seed)
        shuffled: dict[str, tuple[BaseModel, ...]] = {}
        for name, copies in receipts.items():
            events = list(copies)
            for _ in range(rng.randrange(8)):
                events.insert(rng.randrange(len(events) + 1), rng.choice(copies))
            rng.shuffle(events)
            shuffled[name] = tuple(events)
        assert fold(**shuffled).model_dump_json() == expected, seed


def test_all_conflicts_are_sorted_and_distinct() -> None:
    result = fold(**mixed_receipts())
    fact = next(a for a in result.activations if a.activation_id == A5)
    assert fact.state is ActivationState.CONFLICT
    assert fact.conflicts == (
        "activated_copies_differ",
        "installed_and_mismatch",
        "installed_copies_differ",
        "mismatch_copies_differ",
        "not_installed_reasons_differ",
        "session_differs",
    )
    assert fact.candidate_id == C2
    assert fact.session_id == SESSION


def test_later_boundary_preserves_earlier_activation_folds() -> None:
    receipts: dict[str, tuple[BaseModel, ...]] = {
        "prepared": (prepared(), prepared(C2)),
        "activated": (activated(), activated(A2, C2)),
        "installed": (installed(),),
        "install_uncertain": (uncertain(Reason.NATIVE_COMPACTION, A2, "b2"),),
    }
    before = fold(**receipts)
    assert [a.state for a in before.activations] == [
        ActivationState.INSTALLED,
        ActivationState.NOT_INSTALLED,
    ]
    receipts["prepared"] += (prepared(C3),)
    receipts["activated"] += (activated(A3, C3),)
    receipts["installed"] += (installed(A3, "b3"),)
    after = fold(**receipts)
    previous = {a.activation_id: a.model_dump_json() for a in before.activations}
    later = {a.activation_id: a.model_dump_json() for a in after.activations}
    assert {a: later[a] for a in (A1, A2)} == previous
    assert len(after.activations) == 3


def test_fold_models_are_frozen_and_forbid_extra_fields() -> None:
    result = fold(
        prepared=(prepared(),), activated=(activated(),), fallback=(fallback(),)
    )
    for model in (
        ModelCompactionFoldRequest(),
        result,
        *result.candidates,
        *result.activations,
        *result.fallbacks,
    ):
        field = next(iter(type(model).model_fields))
        with pytest.raises(ValidationError, match="frozen_instance"):
            setattr(model, field, getattr(model, field))
        with pytest.raises(ValidationError, match="extra_forbidden"):
            type(model).model_validate({**model.model_dump(), "unexpected": True})


def test_contract_metadata_and_entry_point() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert contract["name"] == NODE_DIR.name
    assert contract["node_type"] == "COMPUTE_GENERIC"
    assert contract["runtime_profiles"] == ["main"]
    assert contract["descriptor"]["purity"] == "pure"
    handler_module = importlib.import_module(contract["handler"]["module"])
    assert (
        getattr(handler_module, contract["handler"]["class"])
        is HandlerContextCompactionReceiptsFold
    )
    for key, model in (
        ("input_model", ModelCompactionFoldRequest),
        ("output_model", ModelCompactionFoldResult),
    ):
        module = importlib.import_module(contract[key]["module"])
        assert getattr(module, contract[key]["name"]) is model
    routing = contract["handler_routing"]["handlers"][0]
    assert routing["handler"]["module"] == contract["handler"]["module"]
    assert routing["handler"]["name"] == HandlerContextCompactionReceiptsFold.__name__
    assert routing["event_model"] == contract["input_model"]
    assert (
        contract["handler"]["input_model"]
        == "omnimarket.models.context_compaction_fold.ModelCompactionFoldRequest"
    )
    completed = "onex.evt.omnimarket.context-compaction-receipts-fold-completed.v1"
    requested = "onex.cmd.omnimarket.context-compaction-receipts-fold-requested.v1"
    assert contract["terminal_event"] == completed
    assert contract["runtime_dispatch"]["terminal_events"]["success"] == completed
    assert contract["runtime_dispatch"]["command_topic"] == requested
    assert contract["event_bus"]["publish_topics"] == [completed]
    assert contract["event_bus"]["subscribe_topics"] == [requested]
    assert contract["capabilities"][0]["name"] == "context.compaction.receipts_fold"
    assert contract["metadata"]["ticket"] == "OMN-17427"
    assert metadata["tickets"] == ["OMN-17427"]
    assert metadata["display_name"] == "Context Compaction Receipts Fold Compute"
    assert metadata["capabilities"]["side_effect_class"] == "none"
    module_name = f"omnimarket.nodes.{NODE_DIR.name}"
    assert metadata["entry_points"]["onex.nodes"][NODE_DIR.name] == module_name
    assert (
        project["project"]["entry-points"]["onex.nodes"][NODE_DIR.name] == module_name
    )
    node_module = importlib.import_module(f"{module_name}.node")
    assert (
        node_module.NodeContextCompactionReceiptsFoldCompute.__name__
        == "NodeContextCompactionReceiptsFoldCompute"
    )
