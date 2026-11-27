# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every handler_*.py of the orchestrator node is bound by its contract (OMN-26721).

The canonical-file-shape ratchet refuses a ``handler_*.py`` that no ``contract.yaml``
binds. The fold engine is not a handler: it lives under ``orchestration/`` and the
contract binds the thin fold handler that reaches it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_lane_work_orchestrator"
)


def _bound_modules(node_dir: Path) -> set[str]:
    document = yaml.safe_load((node_dir / "contract.yaml").read_text())
    bound: set[str] = set()
    for route in document["handler_routing"]["handlers"]:
        bound.add(route["handler"]["module"])
    return bound


def test_every_handler_file_is_bound_by_the_contract() -> None:
    bound = _bound_modules(NODE)
    unbound = [
        path.name
        for path in sorted((NODE / "handlers").glob("handler_*.py"))
        if f"omnimarket.nodes.node_lane_work_orchestrator.handlers.{path.stem}"
        not in bound
    ]
    assert unbound == []


def test_the_fold_engine_lives_under_orchestration() -> None:
    assert (NODE / "orchestration" / "lane_work_orchestrator.py").is_file()


def test_the_help_topic_is_declared_as_consumed_outside_the_node() -> None:
    document = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert (
        "onex.cmd.omnimarket.lane-work-help-requested.v1"
        in document["externally_consumed_topics"]
    )
