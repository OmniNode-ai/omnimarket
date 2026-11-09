# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Acceptance tests of the overlay consumer join check (OMN-23150): what differs, what is held outside an overlay, where the settings come from."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_overlay_drift_compute.handlers.handler_overlay_drift_compute import (
    normalize,
)
from omnimarket.nodes.node_overlay_drift_compute.models import (
    ModelOverlayJoinCommand,
)
from omnimarket.nodes.node_overlay_drift_compute.overlay import (
    ModelOverlayDriftComputeOverlay,
)
from omnimarket.nodes.node_overlay_drift_compute.runtime_overlay_drift_compute import (
    main,
)
from omnimarket.nodes.node_overlay_drift_compute.tests.join_support import (
    HOOK_FILE,
    HOST,
    OVERLAY_FILE,
    PARK_LIST_LINE,
    World,
    overlay_document,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)

pytestmark = pytest.mark.unit

COMMAND = ModelOverlayJoinCommand(correlation_id="c-1", write=False)


# AC1 ---------------------------------------------------------------------------------------
def test_consumer_value_matches_overlay_reads_working() -> None:
    result = World().handler(None).handle(COMMAND)
    assert result.verdict.value == "WORKING"
    assert result.agreeing == ("example-min-free",)
    assert result.mismatches == ()
    assert result.park_lists == ()
    assert result.unreadable == ()


def test_consumer_value_matches_overlay_goes_red_when_the_consumer_differs() -> None:
    world = World()
    world.break_plant_a_floor_of_5_gib_in_the_hook()
    result = world.handler(None).handle(COMMAND)
    assert result.verdict.value == "NOT_WORKING"
    (found,) = result.mismatches
    assert found.consumer == "example-min-free"
    assert (found.overlay_value, found.consumer_value) == ("3072", "5")
    assert found.consumer_via == "file"


def test_consumer_value_matches_overlay_when_spelled_in_another_unit() -> None:
    """3 GiB and 3072 MiB are one quantity: the check compares quantities, not spellings."""
    world = World()
    world.break_write_the_hook_default_in_the_overlays_unit()
    result = world.handler(None).handle(COMMAND)
    assert result.verdict.value == "WORKING"


def test_consumer_value_matches_overlay_reads_the_effective_value_when_the_override_is_set() -> (
    None
):
    world = World()
    world.break_set_the_override_variable("5GiB")
    result = world.handler(None).handle(COMMAND)
    assert result.verdict.value == "NOT_WORKING"
    (found,) = result.mismatches
    assert (found.consumer_value, found.consumer_via) == ("5GiB", "env")
    other = World()
    other.break_set_the_override_variable("3072MiB")
    assert other.handler(None).handle(COMMAND).verdict.value == "WORKING"


def test_consumer_value_matches_overlay_unreadable_sources_read_unknown_not_working() -> (
    None
):
    for break_name, causes in (
        (
            "break_make_the_hook_unreadable",
            (
                "example-min-free.consumer:file-missing",
                "example-hook-park-list:file-missing",
            ),
        ),
        (
            "break_remove_the_overlay_key",
            ("example-min-free.overlay:key-not-found",),
        ),
        (
            "break_remove_the_default_from_the_hook",
            ("example-min-free.consumer:pattern-not-found",),
        ),
        (
            "break_use_an_undeclared_unit",
            ("example-min-free.consumer:unit-Xb-not-declared",),
        ),
    ):
        world = World()
        getattr(world, break_name)()
        result = world.handler(None).handle(COMMAND)
        assert result.verdict.value == "UNKNOWN", break_name
        assert result.outcome.value == "source-unreadable", break_name
        assert result.unreadable == causes, break_name
        assert result.failed


def test_consumer_value_matches_overlay_a_mismatch_stays_red_beside_an_unreadable_source() -> (
    None
):
    document = overlay_document()
    document["park_list_scans"] = [
        {"name": "gone", "path": "$EXAMPLE_GONE_FILE", "pattern": "^PARK_LIST="}
    ]
    world = World()
    world.break_plant_a_floor_of_5_gib_in_the_hook()
    handler = world.handler(
        None, ModelOverlayDriftComputeOverlay.model_validate(document)
    )
    result = handler.handle(COMMAND)
    assert result.verdict.value == "NOT_WORKING"
    assert result.unreadable == ("gone:file-missing",)
    assert result.reasons[0] == "consumer-differs-from-overlay-1"


# AC2 ---------------------------------------------------------------------------------------
def test_planted_floor_mismatch_5_gib_against_3072_mib_is_red_and_the_matching_pair_is_green() -> (
    None
):
    planted = World()
    planted.break_plant_a_floor_of_5_gib_in_the_hook()
    red = planted.handler(None).handle(COMMAND)
    assert red.verdict.value == "NOT_WORKING"
    (found,) = red.mismatches
    assert found.overlay_normalized == str(3072 * 1024**2)
    assert found.consumer_normalized == str(5 * 1024**3)
    green = World().handler(None).handle(COMMAND)
    assert green.verdict.value == "WORKING"


def test_planted_floor_mismatch_is_named_with_both_values_on_the_row() -> None:
    world = World()
    world.break_plant_a_floor_of_5_gib_in_the_hook()
    ledger = InMemoryLedger()
    world.handler(ledger).handle(
        ModelOverlayJoinCommand(correlation_id="c-2", write=True)
    )
    (row,) = ledger.lines
    assert "verdict=NOT_WORKING" in row
    assert "mismatched=1" in row
    assert "differing=example-min-free=overlay:3072+consumer:5" in row
    assert (
        "injected fault: A shell default of 5 GiB against an overlay value of 3072 MiB"
        in row
    )


@pytest.mark.parametrize(
    ("raw", "unit", "expected"),
    [
        ("3072", "MiB", str(3072 * 1024**2)),
        ("3", "GiB", str(3 * 1024**3)),
        ("3072MiB", "GiB", str(3072 * 1024**2)),
        ("1.5GiB", None, str(int(1.5 * 1024**3))),
        ("12", None, "number:12"),
        ("alpha", None, "text:alpha"),
    ],
)
def test_normalize_puts_quantities_in_the_base_unit(
    raw: str, unit: str | None, expected: str
) -> None:
    units = {"B": 1, "MiB": 1024**2, "GiB": 1024**3}
    assert normalize(raw, unit, units) == (expected, None)


def test_normalize_names_an_undeclared_unit_instead_of_guessing() -> None:
    assert normalize("3Xb", None, {"B": 1}) == ("", "unit-Xb-not-declared")


# AC3 ---------------------------------------------------------------------------------------
def test_park_list_outside_overlay_is_reported() -> None:
    world = World()
    world.break_add_a_park_list_to_the_hook()
    result = world.handler(None).handle(COMMAND)
    assert result.verdict.value == "NOT_WORKING"
    assert result.mismatches == ()
    (found,) = result.park_lists
    assert (found.scan, found.line, found.text) == (
        "example-hook-park-list",
        4,
        PARK_LIST_LINE,
    )
    assert result.reasons == ("park-list-outside-overlay-1",)


def test_park_list_outside_overlay_absent_reads_green_as_the_control() -> None:
    result = World().handler(None).handle(COMMAND)
    assert result.park_lists == ()
    assert result.scans_checked == 1
    assert result.verdict.value == "WORKING"


def test_park_list_outside_overlay_is_named_on_the_row() -> None:
    world = World()
    world.break_add_a_park_list_to_the_hook()
    ledger = InMemoryLedger()
    world.handler(ledger).handle(
        ModelOverlayJoinCommand(correlation_id="c-3", write=True)
    )
    (row,) = ledger.lines
    assert "park_lists=1" in row
    assert "differing=park-list:example-hook-park-list:line4" in row


# Overlay and CLI ---------------------------------------------------------------------------
def test_overlay_without_a_consumers_key_is_refused_naming_the_key() -> None:
    document = overlay_document()
    del document["consumers"]
    with pytest.raises(ValueError, match="consumers"):
        ModelOverlayDriftComputeOverlay.model_validate(document)


def test_overlay_without_the_park_list_scans_key_is_refused_naming_the_key() -> None:
    document = overlay_document()
    del document["park_list_scans"]
    with pytest.raises(ValueError, match="park_list_scans"):
        ModelOverlayDriftComputeOverlay.model_validate(document)


def test_overlay_refuses_an_undeclared_unit_a_regex_without_a_value_group_and_a_repeated_consumer() -> (
    None
):
    document = overlay_document()
    document["consumers"][0]["consumer_value"]["unit"] = "TiB"
    with pytest.raises(ValueError, match="undeclared unit TiB"):
        ModelOverlayDriftComputeOverlay.model_validate(document)
    document = overlay_document()
    document["consumers"][0]["consumer_value"]["locator"] = "MIN_FREE=(.*)"
    with pytest.raises(ValueError, match="group named value"):
        ModelOverlayDriftComputeOverlay.model_validate(document)
    document = overlay_document()
    document["consumers"].append(document["consumers"][0])
    with pytest.raises(ValueError, match="declared twice"):
        ModelOverlayDriftComputeOverlay.model_validate(document)


def point_environment(
    monkeypatch: pytest.MonkeyPatch, root: Path, document: dict[str, object] | None
) -> None:
    folder = root / "overlays" / "node_overlay_drift_compute"
    folder.mkdir(parents=True)
    if document is not None:
        (folder / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root / "overlays"))
    monkeypatch.setenv("EXAMPLE_HOST_NAME", HOST)
    monkeypatch.delenv("EXAMPLE_MIN_FREE", raising=False)


def write_world(base: Path, world: World) -> dict[str, str]:
    paths = {
        OVERLAY_FILE: str(base / "overlay-file.yaml"),
        HOOK_FILE: str(base / "hook.sh"),
    }
    Path(paths[OVERLAY_FILE]).write_text(world.overlay_text, encoding="utf-8")
    Path(paths[HOOK_FILE]).write_text(world.hook_text, encoding="utf-8")
    return paths


def test_overlay_missing_is_refused_and_no_path_is_guessed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    point_environment(monkeypatch, tmp_path, None)
    assert main(["probe"]) == 3
    captured = capsys.readouterr()
    assert "INDETERMINATE" in captured.err
    assert "READING" not in captured.out


def test_overlay_paths_decide_which_files_are_read_and_a_planted_floor_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World()
    world.break_plant_a_floor_of_5_gib_in_the_hook()
    world.break_add_a_park_list_to_the_hook()
    paths = write_world(tmp_path, world)
    point_environment(monkeypatch, tmp_path, overlay_document(paths))
    assert main(["probe"]) == 0
    out = capsys.readouterr().out
    assert "verdict=NOT_WORKING" in out
    assert "mismatched=1" in out
    assert "park_lists=1" in out
    assert (
        "MISMATCH consumer=example-min-free overlay=3072 consumer_value=5 via=file"
        in out
    )
    assert (
        "PARK-LIST scan=example-hook-park-list line=4 text=PARK_LIST=(lane-a lane-b)"
        in out
    )


def test_overlay_matching_files_read_working_through_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = write_world(tmp_path, World())
    point_environment(monkeypatch, tmp_path, overlay_document(paths))
    assert main(["probe"]) == 0
    assert "verdict=WORKING" in capsys.readouterr().out


def test_overlay_unset_path_variable_reads_unknown_and_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    point_environment(monkeypatch, tmp_path, overlay_document())
    monkeypatch.delenv("EXAMPLE_OVERLAY_FILE", raising=False)
    monkeypatch.delenv("EXAMPLE_HOOK_FILE", raising=False)
    assert main(["probe"]) == 1
    out = capsys.readouterr().out
    assert "verdict=UNKNOWN" in out
    assert "UNREADABLE example-min-free.overlay:EXAMPLE_OVERLAY_FILE-not-set" in out
