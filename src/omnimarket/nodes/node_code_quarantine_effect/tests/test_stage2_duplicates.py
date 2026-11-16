# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The acceptance reads of OMN-23147 (AC1, AC2), each on the in-memory rig and on the real files and git rig."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from omnimarket.nodes.node_code_quarantine_effect.adapters.adapters_stage2 import (
    JsonFileLoadedCopies,
)
from omnimarket.nodes.node_code_quarantine_effect.handlers.handler_stage2_duplicates import (
    HandlerStage2Duplicates,
)
from omnimarket.nodes.node_code_quarantine_effect.models import (
    EnumQuarantineOutcome,
    EnumStage2Disposition,
    EnumStage2Mode,
)
from omnimarket.nodes.node_code_quarantine_effect.protocols import (
    LoadedCopyUnreadableError,
)
from omnimarket.nodes.node_code_quarantine_effect.tests.support_stage2 import (
    HOME,
    KEPT,
    MUTATIONS,
    NAME,
    NODE_FILES,
    NODE_TESTS,
    SPARE,
    Stage2Rig,
    copy_of,
    files_stage2_rig,
    group_of,
    memory_stage2_rig,
    mutated_stage2_class,
    stage2_request,
)

pytestmark = pytest.mark.unit

RigMaker = Callable[
    [Mapping[str, str | None], type[HandlerStage2Duplicates]], Stage2Rig
]


@pytest.fixture(params=["in-memory", "files"])
def make_stage2_rig(request: pytest.FixtureRequest, tmp_path: Path) -> RigMaker:
    if request.param == "in-memory":
        return lambda loaded, cls: memory_stage2_rig(loaded, cls)
    return lambda loaded, cls: files_stage2_rig(tmp_path, loaded, cls)


def _all_present(rig: Stage2Rig, repository: str) -> bool:
    return all(rig.exists(repository, p) for p in (*NODE_FILES, *NODE_TESTS, KEPT))


def _none_present(rig: Stage2Rig, repository: str) -> bool:
    return not any(rig.exists(repository, p) for p in (*NODE_FILES, *NODE_TESTS))


def check_stage_2_loaded_copy_kept(rig: Stage2Rig) -> None:
    group = group_of()
    quarantined = rig.handler.handle(
        stage2_request(EnumStage2Mode.QUARANTINE, (group,))
    )
    (read,) = quarantined.groups
    assert read.disposition is EnumStage2Disposition.LOADED_COPY_KEPT
    assert read.loaded_repository == HOME
    by_repo = {c.repository: c for c in read.copies}
    assert by_repo[HOME].kept
    assert by_repo[HOME].outcome is None
    assert not by_repo[SPARE].kept
    assert by_repo[SPARE].outcome is EnumQuarantineOutcome.QUARANTINED
    # Quarantine deletes nothing.
    assert _all_present(rig, HOME)
    assert _all_present(rig, SPARE)
    rig.clock.advance(days=7)
    removed = rig.handler.handle(stage2_request(EnumStage2Mode.REMOVE, (group,)))
    assert removed.ok
    (gone,) = removed.groups
    assert {c.repository: c.outcome for c in gone.copies} == {
        HOME: None,
        SPARE: EnumQuarantineOutcome.REMOVED,
    }
    # The spare copy and its tests are gone; the loaded copy and its tests stay.
    assert _none_present(rig, SPARE)
    assert rig.exists(SPARE, KEPT)
    assert _all_present(rig, HOME)
    assert rig.events() == []


def check_stage_2_unread_copy_kept(rig: Stage2Rig) -> None:
    group = group_of()
    for mode in (EnumStage2Mode.QUARANTINE, EnumStage2Mode.REMOVE):
        rig.clock.advance(days=7)
        result = rig.handler.handle(
            stage2_request(mode, (group,), run_id=f"s2{mode.value}")
        )
        (read,) = result.groups
        assert read.disposition is EnumStage2Disposition.KEPT_LOADED_COPY_UNREAD
        assert all(c.kept and c.outcome is None for c in read.copies)
        assert result.unread == (NAME,)
        assert _all_present(rig, HOME)
        assert _all_present(rig, SPARE)
    events = rig.events()
    assert [e.disposition for e in events] == [
        EnumStage2Disposition.KEPT_LOADED_COPY_UNREAD
    ] * 2
    assert all(e.contract_name == NAME and e.reading == "NOT WORKING" for e in events)


def test_stage_2_loaded_copy_kept(make_stage2_rig: RigMaker) -> None:
    check_stage_2_loaded_copy_kept(
        make_stage2_rig({NAME: HOME}, HandlerStage2Duplicates)
    )


def test_stage_2_unread_copy_kept(make_stage2_rig: RigMaker) -> None:
    # Dev's reading does not cover the name, as with an unreachable dev: absence is not "nothing is loaded".
    check_stage_2_unread_copy_kept(make_stage2_rig({}, HandlerStage2Duplicates))


def test_stage_2_unread_copy_kept_when_its_files_cannot_be_seen(
    make_stage2_rig: RigMaker,
) -> None:
    rig = make_stage2_rig({NAME: HOME}, HandlerStage2Duplicates)
    ghost = group_of().model_copy(
        update={
            "copies": (
                copy_of(HOME).model_copy(
                    update={"entity_paths": ("src/missing/contract.yaml",)}
                ),
                copy_of(SPARE),
            )
        }
    )
    result = rig.handler.handle(stage2_request(EnumStage2Mode.QUARANTINE, (ghost,)))
    assert result.unread == (NAME,)
    assert _all_present(rig, SPARE)


def test_stage_2_loaded_copy_kept_in_either_repository(
    make_stage2_rig: RigMaker,
) -> None:
    rig = make_stage2_rig({NAME: SPARE}, HandlerStage2Duplicates)
    result = rig.handler.handle(
        stage2_request(EnumStage2Mode.QUARANTINE, (group_of(),))
    )
    (read,) = result.groups
    assert read.loaded_repository == SPARE
    by_repo = {c.repository: c for c in read.copies}
    assert by_repo[SPARE].kept
    assert by_repo[HOME].outcome is EnumQuarantineOutcome.QUARANTINED


@pytest.mark.parametrize(
    ("loaded", "disposition"),
    [
        (None, EnumStage2Disposition.KEPT_NO_LOADED_COPY),
        ("repo_elsewhere", EnumStage2Disposition.KEPT_NO_LOADED_COPY),
    ],
)
def test_stage_2_loaded_copy_kept_when_dev_loads_none_of_the_copies(
    make_stage2_rig: RigMaker, loaded: str | None, disposition: EnumStage2Disposition
) -> None:
    rig = make_stage2_rig({NAME: loaded}, HandlerStage2Duplicates)
    result = rig.handler.handle(
        stage2_request(EnumStage2Mode.QUARANTINE, (group_of(),))
    )
    assert result.groups[0].disposition is disposition
    assert _all_present(rig, HOME)
    assert _all_present(rig, SPARE)
    assert [e.disposition for e in rig.events()] == [disposition]


def test_stage_2_spare_copy_with_a_public_consumer_stays(
    make_stage2_rig: RigMaker,
) -> None:
    rig = make_stage2_rig({NAME: HOME}, HandlerStage2Duplicates)
    result = rig.handler.handle(
        stage2_request(EnumStage2Mode.QUARANTINE, (group_of(consumers=1),))
    )
    spare = {c.repository: c for c in result.groups[0].copies}[SPARE]
    assert spare.outcome is EnumQuarantineOutcome.REFUSED_PUBLIC_CONSUMER
    assert _all_present(rig, SPARE)


def test_stage_2_removal_before_the_clock_runs_is_refused(
    make_stage2_rig: RigMaker,
) -> None:
    rig = make_stage2_rig({NAME: HOME}, HandlerStage2Duplicates)
    group = group_of()
    rig.handler.handle(stage2_request(EnumStage2Mode.QUARANTINE, (group,)))
    rig.clock.advance(days=3)
    early = rig.handler.handle(
        stage2_request(EnumStage2Mode.REMOVE, (group,), run_id="early")
    )
    spare = {c.repository: c for c in early.groups[0].copies}[SPARE]
    assert spare.outcome is EnumQuarantineOutcome.REFUSED_CLOCK_RUNNING
    assert _all_present(rig, SPARE)
    notag = rig.handler.handle(
        stage2_request(
            EnumStage2Mode.REMOVE, (group,), run_id="notag", tag="never-made"
        )
    )
    assert {c.repository: c for c in notag.groups[0].copies}[SPARE].outcome in {
        EnumQuarantineOutcome.REFUSED_CLOCK_RUNNING,
        EnumQuarantineOutcome.REFUSED_NO_TAG,
    }
    assert _all_present(rig, SPARE)


def test_stage_2_one_unread_group_does_not_hold_back_a_read_one(
    make_stage2_rig: RigMaker,
) -> None:
    rig = make_stage2_rig({NAME: HOME}, HandlerStage2Duplicates)
    unread = group_of("node_dup_unread")
    result = rig.handler.handle(
        stage2_request(EnumStage2Mode.QUARANTINE, (group_of(), unread))
    )
    assert {g.contract_name: g.disposition for g in result.groups} == {
        NAME: EnumStage2Disposition.LOADED_COPY_KEPT,
        "node_dup_unread": EnumStage2Disposition.KEPT_LOADED_COPY_UNREAD,
    }


def test_stage_2_json_reading_missing_malformed_and_unlisted_are_unread(
    tmp_path: Path,
) -> None:
    missing = JsonFileLoadedCopies(tmp_path / "none.json")
    with pytest.raises(LoadedCopyUnreadableError):
        missing.loaded_repository(NAME)
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    with pytest.raises(LoadedCopyUnreadableError):
        JsonFileLoadedCopies(bad).loaded_repository(NAME)
    good = tmp_path / "good.json"
    good.write_text(
        f'{{"loaded_contracts": {{"{NAME}": "{HOME}", "node_nothing": null}}}}',
        encoding="utf-8",
    )
    reader = JsonFileLoadedCopies(good)
    assert reader.loaded_repository(NAME) == HOME
    assert reader.loaded_repository("node_nothing") == ""
    with pytest.raises(LoadedCopyUnreadableError):
        reader.loaded_repository("node_unlisted")


_RED_CASES: list[tuple[str, Callable[[Stage2Rig], None], Mapping[str, str | None]]] = [
    ("quarantine-the-loaded-copy", check_stage_2_loaded_copy_kept, {NAME: HOME}),
    ("skip-the-loaded-copy-reading", check_stage_2_unread_copy_kept, {}),
]


@pytest.mark.parametrize(
    ("mutation", "check", "loaded"), _RED_CASES, ids=[c[0] for c in _RED_CASES]
)
def test_stage_2_mutation_turns_the_acceptance_read_red(
    make_stage2_rig: RigMaker,
    mutation: str,
    check: Callable[[Stage2Rig], None],
    loaded: Mapping[str, str | None],
) -> None:
    assert mutation in MUTATIONS
    with pytest.raises(AssertionError):
        check(make_stage2_rig(loaded, mutated_stage2_class(mutation)))
