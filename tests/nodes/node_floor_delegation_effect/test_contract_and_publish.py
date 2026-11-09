# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract's declarations and how the floor publish carries the tracker (OMN-23081)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    NODE_DIR,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishContract,
    group_steps,
)

from .support import OVERLAY_FILE, RT

PUBLISH_CONTRACT = NODE_DIR / "contract.yaml"
PAGES = NODE_DIR / "page"


def _publish() -> Any:
    raw = yaml.safe_load(PUBLISH_CONTRACT.read_text(encoding="utf-8"))
    return ModelFactoryFloorPublishContract.model_validate(raw).runtime


def test_the_nine_scenarios_and_the_plan_range_are_declared() -> None:
    assert [d.id for d in RT.scenarios] == [f"R{i}" for i in range(1, 10)]
    assert (RT.plan_ticket_first, RT.plan_ticket_last) == (23161, 23183)
    assert {a.id for a in RT.operator_answers} == {"Q1", "Q2", "Q3"}
    builders = [d.builder_ticket for d in RT.scenarios if d.builder_ticket]
    assert all(
        RT.plan_ticket_first <= int(b.split("-")[1]) <= RT.plan_ticket_last
        for b in builders
    )


def test_the_tracker_is_a_publish_step_in_the_first_group_and_the_cycle_still_fits_its_bound() -> (
    None
):
    rt = _publish()
    names = [s.name for s in rt.steps]
    assert names.index("delegation_tracker") < names.index(rt.record_step)
    path = rt.declared_longest_path()
    assert sum(path.values()) <= rt.cycle.bound_seconds, path
    step = next(s for s in rt.steps if s.name == "delegation_tracker")
    assert step.concurrent
    assert not step.required
    group = next(g for g in group_steps(rt.steps) if step in g)
    assert step.timeout_seconds <= max(s.timeout_seconds for s in group)
    # The stage's own deadline plus one read's time limit stays inside the step's time limit.
    assert RT.scenario_stage_deadline_seconds + 10 <= step.timeout_seconds
    assert step.args[0] == "run"


def test_the_tracker_files_are_named_beside_the_other_pages() -> None:
    rt = _publish()
    assert rt.names.delegation_tracker_page == "delegation.html"
    assert rt.names.delegation_tracker_data == "delegation-tracker.json"
    assert rt.names.delegation_tracker_data != rt.names.delegation
    assert rt.delegation_tracker_data_relpath == RT.output_relpath
    assert (NODE_DIR / rt.delegation_tracker_page_relpath).is_file()


def test_every_page_carries_the_delegation_tab() -> None:
    for name in ("simple.html", "compare.html", "delegation.html"):
        html = (PAGES / name).read_text(encoding="utf-8")
        assert 'id="tab-delegation"' in html, name
        assert 'href="delegation.html"' in html, name
    page = (PAGES / "delegation.html").read_text(encoding="utf-8")
    assert 'id="tab-delegation" href="delegation.html" aria-selected="true"' in page


def test_the_page_reads_the_file_the_publish_serves() -> None:
    page = (PAGES / "delegation.html").read_text(encoding="utf-8")
    assert f'DATA = "{_publish().names.delegation_tracker_data}"' in page


def test_the_page_opens_every_number_and_pages_the_ticket_list() -> None:
    page = (PAGES / "delegation.html").read_text(encoding="utf-8")
    for key in (
        "scs:",
        "grp:",
        "pr:red",
        "pr:green",
        "start:true",
        "start:false",
        "scn:",
        "tk:",
        "ship",
    ):
        assert key in page, key
    assert 'type="search"' in page
    assert "details_page_size" in page


def test_the_contract_names_no_home_directory_and_no_person() -> None:
    text = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_floor_delegation_effect/contract.yaml"
    ).read_text(encoding="utf-8")
    assert "/Users/" not in text
    assert "/home/" not in text


def test_the_overlay_is_required_and_a_broken_one_refuses_by_name(
    tmp_path: Path,
) -> None:
    from omnimarket.handlers.node_overlay_reader import NodeOverlayError
    from omnimarket.nodes.node_floor_delegation_effect.handlers.handler_floor_delegation import (
        load_runtime,
    )

    with pytest.raises(NodeOverlayError, match="node_floor_delegation_effect"):
        load_runtime(environ={})
    broken = tmp_path / "overlay.yaml"
    broken.write_text(
        "github_url: https://x.invalid\nunknown_field: 1\n", encoding="utf-8"
    )
    with pytest.raises(NodeOverlayError):
        load_runtime(environ={RT.overlay_file_env: str(broken)})
    good = load_runtime(environ={RT.overlay_file_env: str(OVERLAY_FILE)})
    assert [d.id for d in good.scenarios] == [d.id for d in RT.scenarios]


def test_the_package_names_no_repository_workflow_or_tracker_address() -> None:
    root = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_floor_delegation_effect"
    )
    text = "\n".join(
        p.read_text(encoding="utf-8")
        for p in root.rglob("*")
        if p.suffix in {".py", ".yaml"}
    )
    for literal in ("https://github.com", "linear.app", "OmniNode-ai/"):
        assert literal not in text, literal
