# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection-writer contract gate, proven on synthetic trees and the real one.

Each refusal is driven by a tree built in ``tmp_path`` carrying the exact shape
under test, and each is paired with a control that must stay green, so a gate
that refuses everything cannot pass for one that refuses the defect.

The last group binds the gate to the runtime itself: the key it demands is the
key ``omnibase_infra`` ``_extract_rows_upserted`` reads, and the routing it
refuses is the routing ``_topics_for_handler_entry`` dispatches.

Related Tickets:
    - OMN-19833: rows_written counted as zero writes; a routed fold beside a writer
"""

from __future__ import annotations

import importlib
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
import yaml
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _extract_rows_upserted,
    _is_standalone_projection_runner,
    _topics_for_handler_entry,
)

from omnimarket.projection.runner import BaseProjectionRunner
from omnimarket.validators import projection_writer_contract as gate

pytestmark = pytest.mark.unit

_NODES_ROOT = Path(gate.__file__).resolve().parent.parent / "nodes"
_TOPIC = "onex.evt.test.thing-happened.v1"


def _node(
    nodes_root: Path,
    name: str,
    *,
    writer_body: str,
    writer_flag: bool = True,
    route_fold: bool = False,
) -> None:
    """One synthetic projection node: a writer module, optionally a routed fold."""
    node_dir = nodes_root / name
    (node_dir / "handlers").mkdir(parents=True, exist_ok=True)
    flag = "    onex_runtime_inprocess_dispatch = True\n" if writer_flag else ""
    (node_dir / "handlers" / "writer.py").write_text(
        "from omnimarket.projection.runner import BaseProjectionRunner\n\n"
        "class ThingWriter(BaseProjectionRunner):\n"
        + flag
        + "    def handle(self, input_data):\n"
        + dedent(writer_body).replace("\n", "\n        ").join(["        ", "\n"])
        + "\nclass ThingFold:\n    def handle(self, request):\n        return request\n",
        encoding="utf-8",
    )
    handlers = [
        {
            "operation": "thing_writer",
            "handler": {
                "name": "ThingWriter",
                "module": f"omnimarket.nodes.{name}.handlers.writer",
            },
        }
    ]
    if route_fold:
        handlers.insert(
            0,
            {
                "operation": "thing_fold",
                "handler": {
                    "name": "ThingFold",
                    "module": f"omnimarket.nodes.{name}.handlers.writer",
                },
            },
        )
    (node_dir / "contract.yaml").write_text(
        yaml.safe_dump(
            {"name": name, "handler_routing": {"handlers": handlers}},
        ),
        encoding="utf-8",
    )


GOOD = 'return {"rows_upserted": 1, "rows_refused_by_ordering_guard": 0}'


# --------------------------------------------------------------------------
# Refusal 1: the count key.
# --------------------------------------------------------------------------


def test_a_writer_reporting_rows_written_is_refused(tmp_path: Path) -> None:
    _node(tmp_path, "node_a", writer_body='return {"rows_written": 1}')
    findings, writers = gate.scan(tmp_path)
    assert writers == 1
    messages = " ".join(f.render() for f in findings)
    assert "never reports 'rows_upserted'" in messages
    assert "'rows_written'" in messages


def test_a_writer_reporting_rows_upserted_is_clean(tmp_path: Path) -> None:
    _node(tmp_path, "node_a", writer_body=GOOD)
    assert gate.scan(tmp_path) == ([], 1)


@pytest.mark.parametrize(
    "near_miss", ["rows_inserted", "rows_updated", "rows_affected", "rows_projected"]
)
def test_a_near_miss_key_alone_is_refused_and_beside_the_right_one_is_not(
    tmp_path: Path, near_miss: str
) -> None:
    _node(tmp_path, "node_a", writer_body=f'return {{"{near_miss}": 1}}')
    findings, _ = gate.scan(tmp_path)
    assert [f for f in findings if near_miss in f.message]

    beside = tmp_path / "beside"
    beside.mkdir()
    _node(
        beside, "node_a", writer_body=f'return {{"rows_upserted": 1, "{near_miss}": 1}}'
    )
    assert gate.scan(beside) == ([], 1)


def test_a_named_constant_key_is_resolved(tmp_path: Path) -> None:
    _node(tmp_path, "node_a", writer_body="return {WRONG: 1}")
    writer = tmp_path / "node_a" / "handlers" / "writer.py"
    writer.write_text('WRONG = "rows_written"\n' + writer.read_text(encoding="utf-8"))
    assert [f for f in gate.scan(tmp_path)[0] if "'rows_written'" in f.message]

    good = tmp_path / "good"
    good.mkdir()
    _node(good, "node_a", writer_body="return {KEY: 1}")
    writer = good / "node_a" / "handlers" / "writer.py"
    writer.write_text('KEY = "rows_upserted"\n' + writer.read_text(encoding="utf-8"))
    assert gate.scan(good) == ([], 1)


def test_a_keyword_argument_result_model_counts(tmp_path: Path) -> None:
    _node(tmp_path, "node_a", writer_body="return Result(rows_upserted=1)")
    assert gate.scan(tmp_path) == ([], 1)


def test_a_runner_that_does_not_declare_inprocess_dispatch_is_out_of_scope(
    tmp_path: Path,
) -> None:
    """A standalone runner owns its consume loop; the runtime never counts it."""
    _node(
        tmp_path,
        "node_a",
        writer_body='return {"rows_written": 1}',
        writer_flag=False,
        route_fold=True,
    )
    assert gate.scan(tmp_path) == ([], 0)


# --------------------------------------------------------------------------
# Refusal 2: a routed pure fold beside an in-process writer.
# --------------------------------------------------------------------------


def test_a_routed_fold_beside_a_writer_is_refused(tmp_path: Path) -> None:
    _node(tmp_path, "node_a", writer_body=GOOD, route_fold=True)
    findings, _ = gate.scan(tmp_path)
    assert [f.subject for f in findings] == ["ThingFold"]
    assert (
        "routed in handler_routing beside an in-process writer" in findings[0].message
    )


def test_the_writer_alone_is_clean(tmp_path: Path) -> None:
    _node(tmp_path, "node_a", writer_body=GOOD, route_fold=False)
    assert gate.scan(tmp_path) == ([], 1)


# --------------------------------------------------------------------------
# The floor.
# --------------------------------------------------------------------------


def test_a_scan_that_finds_too_few_writers_fails_the_gate(tmp_path: Path) -> None:
    _node(tmp_path, "node_a", writer_body=GOOD)
    assert gate.main(["--nodes-root", str(tmp_path)]) == 1


def test_a_refusal_fails_the_gate_even_over_the_floor(tmp_path: Path) -> None:
    for index in range(gate.MIN_WRITERS):
        _node(tmp_path, f"node_{index}", writer_body=GOOD)
    assert gate.main(["--nodes-root", str(tmp_path)]) == 0
    _node(tmp_path, "node_bad", writer_body='return {"rows_written": 1}')
    assert gate.main(["--nodes-root", str(tmp_path)]) == 1


# --------------------------------------------------------------------------
# The real tree, and the runtime it must agree with.
# --------------------------------------------------------------------------


def test_the_real_node_tree_is_clean() -> None:
    findings, writers = gate.scan(_NODES_ROOT)
    assert writers >= gate.MIN_WRITERS
    assert [f.render() for f in findings] == []


def test_the_key_the_gate_demands_is_the_key_the_runtime_counts() -> None:
    assert _extract_rows_upserted({gate.ROWS_UPSERTED_KEY: 3}) == 3
    assert _extract_rows_upserted({"rows_written": 3}) == 0, (
        "the defect: a count under any other key reads as zero"
    )


def _runtime_writers() -> list[tuple[str, type[BaseProjectionRunner]]]:
    found: list[tuple[str, type[BaseProjectionRunner]]] = []
    for path in sorted(_NODES_ROOT.glob("*/contract.yaml")):
        contract: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        for name, module in gate._routed_entries(contract):
            cls = getattr(importlib.import_module(module), name)
            if (
                issubclass(cls, BaseProjectionRunner)
                and getattr(cls, gate.INPROCESS_ATTR, False) is True
            ):
                found.append((path.parent.name, cls))
    return found


def test_the_gates_writers_are_the_runtimes_in_process_writers() -> None:
    """The syntax-tree reading of "writer" agrees with the runtime's predicate."""
    runtime = _runtime_writers()
    _, gate_count = gate.scan(_NODES_ROOT)
    assert gate_count == len(runtime) >= gate.MIN_WRITERS
    for _, cls in runtime:
        assert _is_standalone_projection_runner(cls.__new__(cls)) is False


def test_no_dispatched_entry_in_a_writer_node_is_a_fold() -> None:
    """Asked of the runtime's own routing: every entry that receives topics is the writer."""
    writer_nodes = {node for node, _ in _runtime_writers()}
    manifest = discover_contracts_from_paths(
        sorted(_NODES_ROOT.glob("*/contract.yaml"))
    )
    checked = 0
    for contract in manifest.contracts:
        if contract.handler_routing is None:
            continue
        node = Path(contract.contract_path).parent.name
        if node not in writer_nodes:
            continue
        checked += 1
        for entry in contract.handler_routing.handlers:
            if not _topics_for_handler_entry(contract, entry):
                continue
            cls = getattr(
                importlib.import_module(entry.handler.module), entry.handler.name
            )
            assert issubclass(cls, BaseProjectionRunner), (
                f"{node}: {entry.handler.name} is dispatched beside the writer"
            )
    assert checked >= gate.MIN_WRITERS
