# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The terminal guard of the lane_results classes: a lane goes only once its TERMINAL is old.

A lane_results entry the guard holds stays until the lane's newest TERMINAL on the rolling work
ledger is older than terminal_keep_days; an entry whose name names no lane, a lane with no
TERMINAL, and a lane whose ledger cannot be read are all kept, and nothing of a class with an
unreadable ledger is removed. Every host, path and number below is fixture data. Each kept
reason also has its control: the same scene without the guard's input removes the entry, which
shows the guard (and not the age rule) is what kept it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_temp_retention_effect.handlers.handler_temp_retention import (
    tree_bytes,
)
from tests.nodes.node_host_retention_effect.host_retention_fakes import (
    DAY,
    LOADER_MODULE,
    NOW,
    FakeClosure,
    entry,
    loader_fields,
    make_ports,
    results_class,
    run,
)

pytestmark = pytest.mark.unit


def kept_reasons(result: Any) -> dict[str, str]:
    return {Path(k.path).name: k.reason for k in result.kept}


def lane_root(tmp_path: Path) -> Path:
    """A lane_results root whose leaf is one of the names the contract allows."""
    root = tmp_path / "remote-lanes"
    root.mkdir()
    return root


def closed_scene(
    tmp_path: Path,
) -> tuple[Path, FakeClosure, dict[str, Path], dict[str, Any]]:
    """A remote-lanes root of six entries: two closed old lanes go, four stay, each for its own reason."""
    root = tmp_path / "remote-lanes"
    root.mkdir(parents=True)
    entries = {
        "lane-alpha": entry(root, "lane-alpha", days=10),
        "lane-beta": entry(root, "lane-beta", days=10, files=2),
        "lane-young": entry(root, "lane-young", days=2),
        "lane-delta": entry(root, "lane-delta", days=10),
        "lane-gamma": entry(root, "lane-gamma", days=10),
        "result-plain": entry(root, "result-plain", days=10),
    }
    closure = FakeClosure(
        {
            "alpha": NOW - 10 * DAY,
            "beta": NOW - 12 * DAY,
            "young": NOW - 10 * DAY,
            "gamma": NOW - 2 * DAY,
        }
    )
    fields = {
        "age_classes": [results_class(root, lane_pattern="lane-(?P<lane>[a-z]+)")]
    }
    return root, closure, entries, fields


# ---------------------------------------------------------------------------------- dry run


def test_dry_run_lists_closed_old_lanes_with_bytes_and_removes_nothing(
    tmp_path: Path,
) -> None:
    _root, closure, entries, fields = closed_scene(tmp_path)
    sizes = {name: tree_bytes(path) for name, path in entries.items()}

    result = run(tmp_path, fields, ports=make_ports(closure=closure), dry_run=True)

    assert result.status == "ok"
    assert [Path(i.path).name for i in result.items] == ["lane-alpha", "lane-beta"]
    assert result.items[0].bytes == sizes["lane-alpha"]
    assert result.items[1].bytes == sizes["lane-beta"]
    assert result.total_bytes == sizes["lane-alpha"] + sizes["lane-beta"]
    assert all(path.exists() for path in entries.values())
    assert kept_reasons(result) == {
        "lane-young": "too_new",
        "lane-delta": "finished_unknown",
        "lane-gamma": "terminal_too_new",
        "result-plain": "lane_unnamed",
    }


def test_execute_removes_exactly_what_the_dry_run_listed(tmp_path: Path) -> None:
    _root, closure, entries, fields = closed_scene(tmp_path)
    ports = make_ports(closure=closure)

    dry = run(tmp_path, fields, ports=ports, dry_run=True)
    assert all(path.exists() for path in entries.values())
    real = run(tmp_path, fields, ports=ports)

    assert [i.path for i in real.items] == [i.path for i in dry.items]
    assert real.total_bytes == dry.total_bytes
    assert real.errors == ()
    listed = {Path(i.path).name for i in dry.items}
    assert listed == {"lane-alpha", "lane-beta"}
    for name, path in entries.items():
        assert path.exists() is (name not in listed), name


# -------------------------------------------------------------------------------------- kept


def test_kept_a_lane_without_a_terminal_is_never_removed(tmp_path: Path) -> None:
    root = lane_root(tmp_path)
    old = entry(root, "lane-omega", days=10)
    fields = {"age_classes": [results_class(root)]}

    result = run(tmp_path, fields, ports=make_ports(closure=FakeClosure({})))

    assert old.exists()
    assert kept_reasons(result) == {"lane-omega": "finished_unknown"}


def test_kept_a_terminal_younger_than_the_window(tmp_path: Path) -> None:
    root = lane_root(tmp_path)
    old = entry(root, "lane-omega", days=10)
    fields = {"age_classes": [results_class(root)]}
    closure = FakeClosure({"lane-omega": NOW - 2 * DAY})

    result = run(tmp_path, fields, ports=make_ports(closure=closure))

    assert old.exists()
    assert kept_reasons(result) == {"lane-omega": "terminal_too_new"}


def test_kept_an_unreadable_ledger_removes_nothing(tmp_path: Path) -> None:
    root = lane_root(tmp_path)
    old = entry(root, "lane-omega", days=10)
    fields = {"age_classes": [results_class(root)]}

    result = run(tmp_path, fields, ports=make_ports(closure=FakeClosure(None)))

    assert result.status == "ok"
    assert result.items == ()
    assert result.errors == ("receipts-a: ledger unreadable",)
    assert old.exists()


# ------------------------------------------------------------------------------------ control


def test_control_without_the_terminal_guard_the_age_rule_alone_removes_the_entry(
    tmp_path: Path,
) -> None:
    root = lane_root(tmp_path)
    old = entry(root, "lane-omega", days=10)
    unguarded = {
        "name": "receipts-a",
        "kind": "lane_results",
        "root": str(root),
        "keep_days": 7,
    }

    result = run(tmp_path, {"age_classes": [unguarded]})

    assert not old.exists(), (
        "with the terminal block gone the age rule alone removes the entry"
    )
    assert result.errors == ()


# ----------------------------------------------------------------------------- one loader run


def test_the_loader_runs_once_for_classes_that_share_it(tmp_path: Path) -> None:
    west = tmp_path / "west" / "remote-lanes"
    east = tmp_path / "east" / "remote-lanes"
    west.mkdir(parents=True)
    east.mkdir(parents=True)
    entry(west, "lane-omega", days=10)
    entry(east, "lane-theta", days=10)
    closure = FakeClosure({"lane-omega": NOW - 10 * DAY})
    fields = {
        "age_classes": [
            results_class(west, name="receipts-west"),
            results_class(east, name="receipts-east"),
        ]
    }

    result = run(tmp_path, fields, ports=make_ports(closure=closure))

    assert len(closure.calls) == 1
    assert closure.calls[0].argv[-2:] == ("-m", LOADER_MODULE)
    assert not (west / "lane-omega").exists()
    assert kept_reasons(result) == {"lane-theta": "finished_unknown"}


# ------------------------------------------------------------------------------------ refusals


def refused_classes(tmp_path: Path) -> list[tuple[str, dict[str, Any], str]]:
    """A terminal declaration the contract bounds or the overlay model refuses, and a string its refusal names.

    The overlay model names the invalid field path and never the value (a document may carry private facts), so those cases
    assert the path."""
    root = tmp_path / "remote-lanes"
    return [
        (
            "loader command of another module",
            results_class(
                root, loader=loader_fields(argv=["python", "-m", "json.tool"])
            ),
            "must end with -m",
        ),
        (
            "window above the contract maximum",
            results_class(root, loader=loader_fields(window_hours=721)),
            "above the contract maximum",
        ),
        (
            "window shorter than the TERMINAL age",
            results_class(root, loader=loader_fields(window_hours=100)),
            "cannot hold a TERMINAL",
        ),
        (
            "terminal_keep_days",
            results_class(root, terminal_keep_days=0.5),
            "terminal_keep_days",
        ),
        (
            "terminal on lane_scratch",
            {
                **results_class(root),
                "kind": "lane_scratch",
                "root": str(tmp_path / "scratch"),
            },
            "age_classes.0",
        ),
        (
            "pattern without a lane group",
            results_class(root, lane_pattern="(?P<name>.+)"),
            "age_classes.0.terminal",
        ),
        (
            "pattern that does not compile",
            results_class(root, lane_pattern="lane-(?P<lane>["),
            "age_classes.0.terminal",
        ),
    ]


def test_control_a_valid_terminal_declaration_is_accepted(tmp_path: Path) -> None:
    root = lane_root(tmp_path)
    entry(root, "lane-omega", days=10)
    fields = {"age_classes": [results_class(root)]}

    result = run(tmp_path, fields, ports=make_ports(closure=FakeClosure({})))

    assert result.status == "ok"


@pytest.mark.parametrize("case", range(7))
def test_an_unsafe_terminal_declaration_is_refused_and_nothing_is_removed(
    tmp_path: Path, case: int
) -> None:
    name, age_class, needle = refused_classes(tmp_path)[case]
    root = Path(age_class["root"])
    root.mkdir(parents=True, exist_ok=True)
    old = entry(root, "lane-omega", days=10)

    result = run(tmp_path, {"age_classes": [age_class]})

    assert result.status == "refused", name
    assert result.items == ()
    assert old.exists()
    assert any(needle in refusal for refusal in result.refusals), (
        name,
        result.refusals,
    )
