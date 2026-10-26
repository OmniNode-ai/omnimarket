# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-session current-context fold (plan section 6 boundary algorithm step 5, TA.13).

Install-confirm results for one session arrive in any order and may repeat. The
fold keeps the set of maximal boundary chains and every boundary observation, so
any arrival permutation, with duplicates, gives one state, and a stored state
folded with more results gives the same state as folding them all at once. An
installed activation is recorded retired at the next boundary on the chain; its
installation fact is never revised.
"""

from __future__ import annotations

import itertools
import random
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from omnimarket.models.context_compaction_fold import (
    EnumCurrentContextState,
    ModelCurrentContextProjectionRequest,
    ModelSessionCurrentContext,
    ModelSessionCurrentContextState,
)
from omnimarket.models.context_install_confirm import (
    EnumBoundaryKind,
    EnumInstallFact,
    EnumInstallFactReason,
    ModelBoundaryReading,
    ModelCapturedTranscriptRecord,
    ModelCurrentContext,
    ModelInstallationFact,
    ModelInstallConfirmRequest,
    ModelInstallConfirmResult,
    ModelPublishedActivation,
)
from omnimarket.models.context_pack_render import ModelContextPackRenderMessage
from omnimarket.nodes.node_context_compaction_receipts_fold_compute.handlers.handler_context_current_context_fold import (
    HandlerContextCurrentContextFold,
)
from omnimarket.nodes.node_context_install_confirm_compute.handlers.handler_context_install_confirm import (
    HandlerContextInstallConfirm,
    observed_messages_hash,
)

pytestmark = pytest.mark.unit

SESSION = "844d62fa-0000-4000-8000-000000000001"
OTHER_SESSION = "844d62fa-0000-4000-8000-000000000002"
A1 = UUID("9f3c0000-0000-4000-8000-000000000001")
A2 = UUID("9f3c0000-0000-4000-8000-000000000002")
A3 = UUID("9f3c0000-0000-4000-8000-000000000003")
HASH_1 = "1" * 64
HASH_2 = "2" * 64
HASH_3 = "3" * 64
T0 = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def custom(boundary: str, activation_id: UUID, observed: str) -> ModelBoundaryReading:
    return ModelBoundaryReading(
        boundary_uuid=boundary,
        kind=EnumBoundaryKind.CUSTOM,
        activation_id=activation_id,
        observed_hash=observed,
        replacement_uuids=(f"{boundary}-r0",),
    )


def native(boundary: str) -> ModelBoundaryReading:
    return ModelBoundaryReading(
        boundary_uuid=boundary,
        kind=EnumBoundaryKind.NATIVE,
        replacement_uuids=(f"{boundary}-sum",),
    )


def unreadable(boundary: str) -> ModelBoundaryReading:
    return ModelBoundaryReading(
        boundary_uuid=boundary,
        kind=EnumBoundaryKind.UNREADABLE,
        replacement_uuids=(f"{boundary}-r0",),
    )


def installed(activation_id: UUID, boundary: str, digest: str) -> ModelInstallationFact:
    return ModelInstallationFact(
        activation_id=activation_id,
        fact=EnumInstallFact.INSTALLED,
        boundary_uuid=boundary,
        installed_messages_hash=digest,
        observed_hash=digest,
    )


def result(
    chain: tuple[str, ...],
    boundaries: tuple[ModelBoundaryReading, ...],
    facts: tuple[ModelInstallationFact, ...] = (),
    session_id: str = SESSION,
) -> ModelInstallConfirmResult:
    """An install-confirm result; ``current`` is informational for the fold."""
    by_uuid = {b.boundary_uuid: b for b in boundaries}
    newest = by_uuid[chain[-1]] if chain else None
    return ModelInstallConfirmResult(
        session_id=session_id,
        boundaries=boundaries,
        facts=facts,
        pending_activation_ids=(),
        current=ModelCurrentContext(
            boundary_uuid=newest.boundary_uuid if newest else None,
            kind=newest.kind if newest else None,
            chain_boundary_uuids=chain,
        ),
    )


# Custom A1 at b1, then native b2, then custom A3 at b3: the same session read
# three times as its capture grew.
E1 = result(("b1",), (custom("b1", A1, HASH_1),), (installed(A1, "b1", HASH_1),))
E2 = result(
    ("b1", "b2"),
    (custom("b1", A1, HASH_1), native("b2")),
    (installed(A1, "b1", HASH_1),),
)
E3 = result(
    ("b1", "b2", "b3"),
    (custom("b1", A1, HASH_1), native("b2"), custom("b3", A3, HASH_3)),
    (installed(A1, "b1", HASH_1), installed(A3, "b3", HASH_3)),
)


def fold(
    events: list[ModelInstallConfirmResult],
    previous: ModelSessionCurrentContextState | None = None,
) -> ModelSessionCurrentContextState:
    return (
        HandlerContextCurrentContextFold()
        .handle(
            ModelCurrentContextProjectionRequest(
                events=tuple(events), previous=previous
            )
        )
        .state
    )


def view(events: list[ModelInstallConfirmResult]) -> ModelSessionCurrentContext:
    return (
        HandlerContextCurrentContextFold()
        .handle(ModelCurrentContextProjectionRequest(events=tuple(events)))
        .current
    )


def test_custom_then_native_retires_the_custom_activation_at_the_native_boundary() -> (
    None
):
    current = view([E1, E2])
    assert current.session_id == SESSION
    assert current.state is EnumCurrentContextState.CURRENT
    assert current.boundary_uuid == "b2"
    assert current.kind is EnumBoundaryKind.NATIVE
    assert current.activation_id is None
    assert current.chain_boundary_uuids == ("b1", "b2")
    [retired] = current.retirements
    assert (
        retired.activation_id,
        retired.installed_at_boundary_uuid,
        retired.retired_at_boundary_uuid,
    ) == (A1, "b1", "b2")
    assert current.conflicts == ()


def test_custom_boundary_with_its_installed_activation_is_current() -> None:
    current = view([E1])
    assert current.state is EnumCurrentContextState.CURRENT
    assert (current.boundary_uuid, current.kind, current.activation_id) == (
        "b1",
        EnumBoundaryKind.CUSTOM,
        A1,
    )
    assert current.retirements == ()


def test_native_then_custom_makes_the_custom_activation_current() -> None:
    current = view([E3])
    assert (current.boundary_uuid, current.activation_id) == ("b3", A3)
    assert [r.activation_id for r in current.retirements] == [A1]


def test_session_with_no_boundary_has_no_current_context() -> None:
    current = view([result((), ())])
    assert current.state is EnumCurrentContextState.NO_BOUNDARY
    assert current.boundary_uuid is None
    assert current.kind is None
    assert current.chain_boundary_uuids == ()
    assert current.retirements == ()


def test_every_permutation_with_duplicates_folds_to_one_state() -> None:
    events = [E1, E2, E3]
    expected = fold(events)
    rng = random.Random(21115)
    for perm in itertools.permutations(events):
        for _ in range(5):
            batch = list(perm) + [rng.choice(events) for _ in range(rng.randint(0, 3))]
            rng.shuffle(batch)
            assert fold(batch) == expected
            assert view(batch) == view(events)


def test_incremental_fold_from_a_stored_state_matches_one_batch() -> None:
    expected = fold([E1, E2, E3])
    for perm in itertools.permutations([E1, E2, E3]):
        state: ModelSessionCurrentContextState | None = None
        for event in perm:
            state = fold([event], previous=state)
        assert state == expected


def test_stored_state_round_trips_through_json() -> None:
    state = fold([E1, E2, E3])
    assert (
        ModelSessionCurrentContextState.model_validate_json(state.model_dump_json())
        == state
    )


def test_a_later_boundary_never_changes_an_earlier_retirement() -> None:
    before = view([E1, E2])
    after = view([E1, E2, E3])
    assert before.retirements[0] == after.retirements[0]
    assert before.retirements[0].model_dump_json() == (
        after.retirements[0].model_dump_json()
    )


def test_refolding_a_duplicate_reports_unchanged() -> None:
    handler = HandlerContextCurrentContextFold()
    first = handler.handle(ModelCurrentContextProjectionRequest(events=(E1, E2)))
    assert first.changed is True
    again = handler.handle(
        ModelCurrentContextProjectionRequest(events=(E2,), previous=first.state)
    )
    assert again.changed is False
    assert again.state == first.state
    later = handler.handle(
        ModelCurrentContextProjectionRequest(events=(E3,), previous=first.state)
    )
    assert later.changed is True


def test_diverging_chains_are_a_conflict_in_every_order() -> None:
    other = result(
        ("b1", "b9"),
        (custom("b1", A1, HASH_1), native("b9")),
        (installed(A1, "b1", HASH_1),),
    )
    for perm in itertools.permutations([E2, other, E1]):
        current = view(list(perm))
        assert current.state is EnumCurrentContextState.CONFLICT
        assert "chains_diverge" in current.conflicts
        assert current.boundary_uuid is None
        assert current.activation_id is None
        assert current.retirements == ()


def test_differing_decided_readings_of_one_boundary_are_a_conflict() -> None:
    other = result(("b1",), (custom("b1", A2, HASH_2),), (installed(A2, "b1", HASH_2),))
    for perm in itertools.permutations([E1, other]):
        current = view(list(perm))
        assert current.state is EnumCurrentContextState.CONFLICT
        assert "boundary_readings_differ" in current.conflicts


def test_undecided_reading_is_resolved_by_a_fuller_capture_in_any_order() -> None:
    # The boundary names A1 but the activation receipt was not read yet.
    early = result(("b1",), (custom("b1", A1, HASH_1),))
    for perm in itertools.permutations([early, E1]):
        current = view(list(perm))
        assert current.state is EnumCurrentContextState.CURRENT
        assert current.activation_id == A1
        assert current.conflicts == ()
    alone = view([early])
    assert alone.state is EnumCurrentContextState.CURRENT
    assert alone.activation_id is None


def test_unreadable_reading_is_resolved_by_a_readable_capture_in_any_order() -> None:
    truncated = result(("b1",), (unreadable("b1"),))
    for perm in itertools.permutations([truncated, E1]):
        current = view(list(perm))
        assert current.state is EnumCurrentContextState.CURRENT
        assert (current.kind, current.activation_id) == (EnumBoundaryKind.CUSTOM, A1)


def test_install_mismatch_at_a_boundary_is_current_without_an_activation() -> None:
    mismatch = result(
        ("b1",),
        (custom("b1", A1, HASH_2),),
        (
            ModelInstallationFact(
                activation_id=A1,
                fact=EnumInstallFact.INSTALL_MISMATCH,
                boundary_uuid="b1",
                installed_messages_hash=HASH_1,
                observed_hash=HASH_2,
            ),
        ),
    )
    current = view([mismatch])
    assert current.state is EnumCurrentContextState.CURRENT
    assert (current.kind, current.activation_id) == (EnumBoundaryKind.CUSTOM, None)
    # A later native boundary retires nothing: nothing was installed at b1.
    later = result(
        ("b1", "b2"),
        (custom("b1", A1, HASH_2), native("b2")),
        mismatch.facts,
    )
    assert view([mismatch, later]).retirements == ()


def test_not_selected_fact_does_not_make_an_activation_current() -> None:
    discarded = result(
        ("b1",),
        (custom("b1", A1, HASH_1),),
        (
            installed(A1, "b1", HASH_1),
            ModelInstallationFact(
                activation_id=A2,
                fact=EnumInstallFact.NOT_INSTALLED,
                reason=EnumInstallFactReason.NOT_SELECTED,
                boundary_uuid="b1",
                installed_messages_hash=HASH_2,
            ),
        ),
    )
    assert view([discarded]).activation_id == A1


def test_results_of_two_sessions_are_refused() -> None:
    with pytest.raises(ValidationError):
        ModelCurrentContextProjectionRequest(
            events=(E1, result((), (), session_id=OTHER_SESSION))
        )
    with pytest.raises(ValidationError):
        ModelCurrentContextProjectionRequest(
            events=(result((), (), session_id=OTHER_SESSION),),
            previous=fold([E1]),
        )
    with pytest.raises(ValidationError):
        ModelCurrentContextProjectionRequest(events=())


# --- the merged install-confirm compute feeds the fold -----------------------


def _rec(
    uuid: str,
    parent: str | None,
    t: int,
    record_type: str = "user",
    text: str | None = "turn",
    **fields: object,
) -> ModelCapturedTranscriptRecord:
    role = record_type if record_type in {"user", "assistant"} else None
    return ModelCapturedTranscriptRecord.model_validate(
        {
            "uuid": uuid,
            "parent_uuid": parent,
            "record_type": record_type,
            "role": role,
            "text": text,
            "timestamp": T0 + timedelta(seconds=t),
            **fields,
        }
    )


def test_install_confirm_results_of_a_growing_capture_fold_to_the_retirement() -> None:
    messages = [
        ("user", "legend and stable blocks"),
        ("assistant", "acknowledged"),
        ("user", f"identity trailer\nactivation_id: {A1}"),
    ]
    digest = observed_messages_hash(
        ModelContextPackRenderMessage(role=role, text=text) for role, text in messages
    )
    records = [
        _rec("p0", None, 0, prompt_source="sdk"),
        _rec("p1", "p0", 1, "assistant", "reply"),
        _rec(
            "b1",
            None,
            10,
            "system",
            None,
            subtype="compact_boundary",
            logical_parent_uuid="p1",
        ),
    ]
    parent = "b1"
    for index, (role, text) in enumerate(messages):
        records.append(_rec(f"b1-r{index}", parent, 10, role, text))
        parent = f"b1-r{index}"
    records.append(
        _rec(
            "b1-caveat",
            parent,
            11,
            "user",
            "<local-command-caveat>x</local-command-caveat>",
            is_meta=True,
        )
    )
    first_capture = list(records)
    records += [
        _rec("u2", "b1-caveat", 20, "user", "operator prompt", prompt_source="sdk"),
        _rec(
            "b2",
            None,
            30,
            "system",
            None,
            subtype="compact_boundary",
            logical_parent_uuid="u2",
            preserved_uuids=("b2-p0",),
        ),
        _rec("b2-sum", "b2", 30, "user", "native summary", is_compact_summary=True),
        _rec("b2-p0", "b2-sum", 30, "user", "kept turn"),
    ]
    published = (
        ModelPublishedActivation(
            activation_id=A1,
            installed_messages_hash=digest,
            published_at=T0 + timedelta(seconds=5),
        ),
    )
    compute = HandlerContextInstallConfirm()
    early, late = (
        compute.handle(
            ModelInstallConfirmRequest(
                session_id=SESSION, records=tuple(capture), activations=published
            )
        )
        for capture in (first_capture, records)
    )
    for perm in ([early, late], [late, early], [late, early, late, early]):
        current = view(perm)
        assert current.state is EnumCurrentContextState.CURRENT
        assert (current.boundary_uuid, current.kind) == ("b2", EnumBoundaryKind.NATIVE)
        assert [
            (r.activation_id, r.installed_at_boundary_uuid, r.retired_at_boundary_uuid)
            for r in current.retirements
        ] == [(A1, "b1", "b2")]
        assert current.retirements == late.current.retirements
