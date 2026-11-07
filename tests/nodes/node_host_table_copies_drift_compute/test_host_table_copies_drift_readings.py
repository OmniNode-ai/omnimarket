# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Acceptance tests of the host table copies drift read (OMN-22809): what differs, and where the paths come from."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_host_table_copies_drift_compute.handlers.handler_host_table_copies_drift import (
    HandlerHostTableCopiesDrift,
    compare_copies,
)
from omnimarket.nodes.node_host_table_copies_drift_compute.models import (
    ModelHostTableDriftCommand,
)
from omnimarket.nodes.node_host_table_copies_drift_compute.runtime_host_table_copies_drift import (
    main,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_host_table_copies_drift_compute.drift_support import (
    COPIES,
    HOST,
    RUN_AT,
    World,
    overlay,
    overlay_document,
    table_text,
)

pytestmark = pytest.mark.unit

COMMAND = ModelHostTableDriftCommand(correlation_id="c-1", write=False)


def values_of(world: World, host: str, field: str) -> dict[str, str]:
    result = world.handler(None).handle(COMMAND)
    (found,) = [d for d in result.differences if d.host == host and d.field == field]
    return {value.copy_name: value.value for value in found.values}


def test_three_copies_differing_in_one_field_of_one_host_name_the_host_field_and_each_value() -> (
    None
):
    world = World()
    world.break_change_a_cap(copy="config", host="host-b", cap=9)
    result = world.handler(None).handle(COMMAND)
    assert result.verdict.value == "NOT_WORKING"
    assert [(d.host, d.field) for d in result.differences] == [("host-b", "cap")]
    assert values_of(world, "host-b", "cap") == {
        "packaged": "6",
        "config": "9",
        "overlays": "6",
    }
    assert result.differing_hosts == 1


def test_identical_copies_report_no_difference() -> None:
    result = World().handler(None).handle(COMMAND)
    assert result.verdict.value == "WORKING"
    assert result.differences == ()
    assert result.hosts_compared == 2


def test_a_field_held_by_one_copy_only_shows_the_absent_token_in_the_others() -> None:
    world = World()
    world.break_add_a_field_to_one_copy("overlays")
    assert values_of(world, "host-b", "unit_slots") == {
        "packaged": "<absent>",
        "config": "<absent>",
        "overlays": "2",
    }


def test_a_host_missing_from_one_copy_is_its_own_difference_and_its_fields_compare_among_holders() -> (
    None
):
    world = World()
    world.break_drop_a_host_from_one_copy("config")
    world.break_change_a_cap(copy="overlays", host="host-b", cap=1)
    result = world.handler(None).handle(COMMAND)
    by_field = {
        (d.host, d.field): {v.copy_name: v.value for v in d.values}
        for d in result.differences
    }
    assert by_field[("host-b", "<host-row>")] == {
        "packaged": "present",
        "config": "<absent>",
        "overlays": "present",
    }
    assert by_field[("host-b", "cap")] == {"packaged": "6", "overlays": "1"}


def test_compare_copies_names_a_nested_value_as_json() -> None:
    tables = {
        "a": {"h": {"clones": [{"repo": "x", "ref": "dev"}]}},
        "b": {"h": {"clones": [{"repo": "x", "ref": "main"}]}},
    }
    differences, compared, differing = compare_copies(
        tables, absent_token="<absent>", host_row_field="<host-row>"
    )
    assert compared == 1
    assert differing == 1
    assert [v.value for v in differences[0].values] == [
        '[{"ref":"dev","repo":"x"}]',
        '[{"ref":"main","repo":"x"}]',
    ]


def test_the_row_names_the_difference_and_carries_the_counts() -> None:
    world = World()
    world.break_change_a_cap()
    world.break_add_a_field_to_one_copy()
    ledger = InMemoryLedger()
    result = world.handler(ledger).handle(
        ModelHostTableDriftCommand(correlation_id="c-2")
    )
    row = ledger.lines[0]
    assert result.ledger == "written"
    assert row.startswith(
        f"{RUN_AT:%Y-%m-%dT%H:%M:%SZ} | STATUS | lane=host-table-copies-drift"
    )
    assert "differing_hosts=2" in row
    assert "differing_fields=2" in row
    assert "host-a.cap=packaged:3+config:6+overlays:3" in row
    assert "host-b.unit_slots=packaged:<absent>+config:<absent>+overlays:2" in row
    assert f"host={HOST}" in row


# The injected fault the overlay names: a cap changed in one copy.
def test_the_injected_fault_turns_the_reading_not_working_and_removing_it_restores_working() -> (
    None
):
    world = World()
    handler: HandlerHostTableCopiesDrift = world.handler(None)
    assert handler.handle(COMMAND).verdict.value == "WORKING"
    world.break_change_a_cap()
    assert world.handler(None).handle(COMMAND).verdict.value == "NOT_WORKING"
    assert overlay().injected_fault == "A cap changed in one copy of the host table"


# --- the copy paths come from the overlay, with no default -------------------------------------


def write_world(base: Path, world: World) -> dict[str, str]:
    paths: dict[str, str] = {}
    for name, text in world.texts().items():
        where = base / f"{name}.yaml"
        where.write_text(text, encoding="utf-8")
        paths[name] = str(where)
    return paths


def point_environment(
    monkeypatch: pytest.MonkeyPatch, root: Path, document: dict[str, object] | None
) -> None:
    folder = root / "overlays" / "node_host_table_copies_drift_compute"
    folder.mkdir(parents=True)
    if document is not None:
        (folder / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root / "overlays"))
    monkeypatch.setenv("EXAMPLE_HOST_NAME", HOST)


def test_overlay_missing_is_refused_and_no_path_is_guessed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    point_environment(monkeypatch, tmp_path, None)
    assert main(["probe"]) == 3
    captured = capsys.readouterr()
    assert "INDETERMINATE" in captured.err
    assert "READING" not in captured.out


def test_overlay_without_a_copies_key_is_refused_naming_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    document = overlay_document()
    del document["copies"]
    point_environment(monkeypatch, tmp_path, document)
    assert main(["probe"]) == 3
    assert "copies" in capsys.readouterr().err


def test_overlay_paths_decide_which_files_are_read_and_a_planted_cap_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World()
    world.break_change_a_cap(copy="overlays", host="host-a", cap=4)
    paths = write_world(tmp_path, world)
    point_environment(monkeypatch, tmp_path, overlay_document(paths))
    assert main(["probe"]) == 0
    out = capsys.readouterr().out
    assert "verdict=NOT_WORKING" in out
    assert "differing_fields=1" in out
    assert "DIFF host=host-a field=cap packaged=3 config=3 overlays=4" in out


def test_overlay_path_naming_an_unset_variable_reads_unknown_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = write_world(tmp_path, World())
    paths["overlays"] = "$EXAMPLE_UNSET_COPY_ROOT/overlay.yaml"
    point_environment(monkeypatch, tmp_path, overlay_document(paths))
    monkeypatch.delenv("EXAMPLE_UNSET_COPY_ROOT", raising=False)
    assert main(["probe"]) == 1
    out = capsys.readouterr().out
    assert "verdict=UNKNOWN" in out
    assert "reason=copy-unreadable-overlays-example-unset-copy-root-is-not-set" in out


def test_overlay_path_with_a_variable_is_read_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = write_world(tmp_path, World())
    paths["overlays"] = "$EXAMPLE_COPY_ROOT/overlays.yaml"
    point_environment(monkeypatch, tmp_path, overlay_document(paths))
    monkeypatch.setenv("EXAMPLE_COPY_ROOT", str(tmp_path))
    assert main(["probe"]) == 0
    assert "verdict=WORKING" in capsys.readouterr().out


def test_every_declared_copy_is_read_and_the_example_overlay_declares_no_more_than_the_test_uses() -> (
    None
):
    assert [copy.name for copy in overlay().copies] == list(COPIES)
    assert table_text([{"name": "h"}]).startswith("hosts:")
