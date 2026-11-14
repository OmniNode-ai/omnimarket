# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one-off register read (OMN-19998): the register against the liveness processes and the nodes that exist.

An unregistered in-scope process, an entry naming a process the liveness does not know and a node
replacement that does not exist each fail the read by name, and the same read without the break
passes. Unplanned and bypass readings are reported, never failures, and a retired entry is not
checked at all. The models refuse the broken registers, and the overlay loaders return typed
models or refuse naming the broken field.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
)
from omnimarket.nodes.node_oneoff_register_compute.handlers.handler_oneoff_register import (
    HandlerOneOffRegister,
)
from omnimarket.nodes.node_oneoff_register_compute.models.model_oneoff_register import (
    ModelLivenessProcessRef,
    ModelOneOffEntry,
    ModelOneOffExclusion,
    ModelOneOffRegisterPolicy,
    ModelOneOffRegisterRequest,
    ModelOneOffRegisterResult,
)
from omnimarket.nodes.node_oneoff_register_compute.overlay import (
    load_liveness_refs,
    load_oneoff_register_policy,
)
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

NODE = "node_start_path_census_compute"
PROCESS = "example-process"


def entry(
    one_off_id: str = "one-off-1",
    state: str = "replacement_live",
    kind: str = "node",
    ref: str = NODE,
    bypass: str = "no",
    process_id: str = PROCESS,
) -> ModelOneOffEntry:
    return ModelOneOffEntry(
        one_off_id=one_off_id,
        what="example one-off",
        where="example-host",
        category="C2",
        owner="example-owner",
        owner_repo="example-repo",
        state=state,
        replacement_kind=kind,
        replacement_ref=ref,
        bypass=bypass,
        liveness_process_id=process_id,
        basis="example basis",
    )


def live(
    process_id: str, host: str = "example-host", state: str = "active"
) -> ModelLivenessProcessRef:
    return ModelLivenessProcessRef(process_id=process_id, host=host, state=state)


def read(
    entries: tuple[ModelOneOffEntry, ...],
    exclusions: tuple[ModelOneOffExclusion, ...] = (),
    liveness: tuple[ModelLivenessProcessRef, ...] = (),
    node_names: tuple[str, ...] = (NODE,),
) -> ModelOneOffRegisterResult:
    return HandlerOneOffRegister().handle(
        ModelOneOffRegisterRequest(
            correlation_id="register-1",
            policy=ModelOneOffRegisterPolicy(
                scope_hosts=("example-host",),
                scope_states=("active",),
                entries=entries,
                exclusions=exclusions,
            ),
            liveness_processes=liveness,
            node_names=node_names,
        )
    )


# ---- the read ----


def test_golden_a_registered_liveness_process_and_a_knowing_replacement_pass() -> None:
    result = read(
        entries=(
            entry("one-off-live"),
            entry(
                "one-off-planned",
                state="replacement_planned",
                kind="plan",
                ref="example-plan",
                process_id="example-process-2",
            ),
        ),
        exclusions=(
            ModelOneOffExclusion(
                process_id="example-process-excluded", reason="example"
            ),
        ),
        liveness=(
            live(PROCESS),
            live("example-process-2"),
            live("example-process-excluded"),
        ),
    )

    assert result.passed is True
    assert (result.one_off_count, result.exclusion_count) == (2, 1)
    assert result.state_counts == (
        ("replacement_live", 1),
        ("replacement_planned", 1),
    )
    assert result.unregistered_processes == ()
    assert result.unknown_processes == ()
    assert result.missing_replacement_nodes == ()


def test_an_unregistered_in_scope_process_fails_the_read_and_the_control_passes() -> (
    None
):
    broken = read(entries=(entry(),), liveness=(live(PROCESS), live("stray")))
    assert broken.passed is False
    assert broken.unregistered_processes == ("stray",)

    clean = read(entries=(entry(),), liveness=(live(PROCESS),))
    assert clean.passed is True


def test_an_entry_naming_a_process_the_liveness_does_not_know_fails() -> None:
    broken = read(entries=(entry(),), liveness=(live("some-other-process"),))
    assert broken.passed is False
    assert broken.unknown_processes == ("one-off-1",)
    assert "process_unknown:example-process" in broken.readings[0].findings

    clean = read(entries=(entry(),), liveness=(live(PROCESS),))
    assert clean.passed is True


def test_an_entry_naming_a_node_that_does_not_exist_fails() -> None:
    the_entry = entry(ref="node_example_missing")
    broken = read(entries=(the_entry,), liveness=(live(PROCESS),), node_names=(NODE,))
    assert broken.passed is False
    assert broken.missing_replacement_nodes == ("one-off-1",)
    assert (
        "replacement_node_missing:node_example_missing" in broken.readings[0].findings
    )

    clean = read(
        entries=(the_entry,),
        liveness=(live(PROCESS),),
        node_names=(NODE, "node_example_missing"),
    )
    assert clean.passed is True


def test_a_process_off_the_scope_is_not_unregistered() -> None:
    result = read(
        entries=(),
        liveness=(live("stray", host="other-host"), live("stray", state="draining")),
    )

    assert result.passed is True
    assert result.unregistered_processes == ()


def test_unplanned_and_bypass_readings_are_reported_and_never_fail() -> None:
    result = read(
        entries=(
            entry(
                "unplanned-1", state="no_replacement", kind="none", ref="", bypass="yes"
            ),
            entry(
                "unread-1",
                state="replacement_planned",
                kind="plan",
                ref="example-plan",
                bypass="unread",
                process_id="example-process-2",
            ),
        ),
        liveness=(live(PROCESS), live("example-process-2")),
    )

    assert result.passed is True
    assert result.unplanned == ("unplanned-1",)
    assert result.bypass_yes == ("unplanned-1",)
    assert result.bypass_unread == ("unread-1",)


def test_a_retired_entry_naming_a_missing_node_and_process_reads_no_findings() -> None:
    result = read(
        entries=(entry(state="retired", ref="node_example_missing"),),
        liveness=(),
        node_names=(NODE,),
    )

    assert result.passed is True
    assert result.readings[0].findings == ()


# ---- the models refuse the broken registers ----


def entry_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "one_off_id": "one-off-1",
        "what": "example one-off",
        "where": "example-host",
        "category": "C2",
        "owner": "example-owner",
        "owner_repo": "example-repo",
        "state": "replacement_live",
        "replacement_kind": "node",
        "replacement_ref": NODE,
        "bypass": "no",
        "liveness_process_id": PROCESS,
        "basis": "example basis",
    }
    document.update(overrides)
    return document


def policy_document(
    entries: list[dict[str, object]],
    exclusions: list[dict[str, object]] = (),
) -> dict[str, object]:
    return {
        "scope_hosts": ["example-host"],
        "scope_states": ["active"],
        "entries": entries,
        "exclusions": exclusions,
    }


@pytest.mark.parametrize(
    ("document", "match"),
    [
        pytest.param(
            policy_document([entry_document(replacement_ref="")]),
            "names no ref",
            id="a_node_kind_names_no_ref",
        ),
        pytest.param(
            policy_document([entry_document(state="no_replacement")]),
            "no_replacement with a",
            id="no_replacement_with_a_node",
        ),
        pytest.param(
            policy_document(
                [entry_document(state="replacement_built", replacement_kind="plan")]
            ),
            "without a node",
            id="replacement_built_with_a_plan",
        ),
        pytest.param(
            policy_document([entry_document(), entry_document()]),
            "more than once",
            id="a_repeated_one_off_id",
        ),
        pytest.param(
            policy_document([entry_document(), entry_document(one_off_id="one-off-2")]),
            "more than one one-off",
            id="one_process_two_entries",
        ),
        pytest.param(
            policy_document(
                [entry_document()],
                exclusions=[{"process_id": PROCESS, "reason": "example"}],
            ),
            "both excluded and registered",
            id="excluded_and_registered",
        ),
        pytest.param(
            {**policy_document([entry_document()]), "bogus": "example"},
            "Extra inputs are not permitted",
            id="an_unknown_field",
        ),
    ],
)
def test_the_models_refuse_broken_registers(
    document: dict[str, object], match: str
) -> None:
    with pytest.raises(ValidationError, match=match):
        ModelOneOffRegisterPolicy.model_validate(document)


# ---- the overlay loaders ----


def test_the_loaders_read_typed_models_from_the_overlays(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_overlay(
        monkeypatch,
        tmp_path,
        "node_oneoff_register_compute",
        {
            "scope_hosts": ["example-host"],
            "scope_states": ["active"],
            "entries": [entry_document()],
            "exclusions": [{"process_id": "example-excluded", "reason": "example"}],
        },
    )
    install_node_overlay(
        monkeypatch,
        tmp_path,
        "automation_liveness",
        {
            "processes": [
                {
                    "process_id": PROCESS,
                    "host": "example-host",
                    "state": "active",
                    "cpu": 0.5,
                }
            ]
        },
    )

    policy = load_oneoff_register_policy()
    assert policy.scope_hosts == ("example-host",)
    assert policy.entries[0].one_off_id == "one-off-1"
    assert policy.exclusions[0].process_id == "example-excluded"

    liveness = load_liveness_refs()
    assert [process.process_id for process in liveness.processes] == [PROCESS]


def test_a_loader_refuses_an_invalid_overlay_naming_the_field(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_overlay(
        monkeypatch,
        tmp_path,
        "node_oneoff_register_compute",
        {
            "scope_hosts": ["example-host"],
            "scope_states": ["active"],
            "entries": [entry_document(state="bogus")],
            "exclusions": [],
        },
    )

    with pytest.raises(NodeOverlayError, match=r"entries\.0\.state"):
        load_oneoff_register_policy()


def test_a_loader_refuses_a_missing_overlay(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "empty"))

    with pytest.raises(NodeOverlayNotFoundError):
        load_oneoff_register_policy()


@pytest.mark.parametrize(
    ("written", "reading"),
    [(False, "no"), (True, "yes"), ("unread", "unread")],
)
def test_a_bare_yes_or_no_in_an_overlay_is_the_reading_not_a_refusal(
    written: object, reading: str
) -> None:
    """YAML reads an unquoted ``no`` as false; the overlay author meant the word."""
    made = entry(bypass=written)  # type: ignore[arg-type]
    assert made.bypass.value == reading
