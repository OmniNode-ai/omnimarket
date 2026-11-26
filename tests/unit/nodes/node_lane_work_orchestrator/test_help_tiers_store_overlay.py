# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The help chain is read from the settings store's root before the overlay file, and a workflow keeps its own copy (OMN-26722)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_lane_work_orchestrator.handlers.handler_lane_work_help import (
    HandlerLaneWorkHelp,
)
from omnimarket.nodes.node_lane_work_orchestrator.handlers.overlay_help_tiers import (
    NODE_NAME,
    HelpTiersError,
    load_help_tiers,
)

from .conftest import build_request, driver_in_help

pytestmark = pytest.mark.unit

STORE_RELPATH = ".local/state/onex/overlay-store"


def _tier(name: str, backend: str) -> dict[str, str]:
    return {
        "name": name,
        "backend_id": backend,
        "harness": "h",
        "model": "m",
        "effort": "high",
    }


def _write(root: Path, document: dict[str, object]) -> None:
    folder = root / NODE_NAME
    folder.mkdir(parents=True)
    (folder / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")


def _environ(tmp_path: Path, declared: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path / "home"),
        "OMNIBASE_PATH": str(tmp_path / "registry" / "root"),
        "ONEX_SKILL_OVERLAY_ROOTS": str(declared),
    }


def test_store_chain_wins_over_the_declared_file(tmp_path: Path) -> None:
    declared = tmp_path / "declared"
    _write(declared, {"help_tiers": [_tier("a", "from-file")]})
    _write(
        tmp_path / "home" / STORE_RELPATH, {"help_tiers": [_tier("b", "from-store")]}
    )
    chain = load_help_tiers(_environ(tmp_path, declared))
    assert [tier.backend_id for tier in chain] == ["from-store"]


def test_declared_file_is_read_when_the_store_holds_no_document_for_the_node(
    tmp_path: Path,
) -> None:
    declared = tmp_path / "declared"
    _write(declared, {"help_tiers": [_tier("a", "from-file")]})
    (tmp_path / "home" / STORE_RELPATH).mkdir(parents=True)
    chain = load_help_tiers(_environ(tmp_path, declared))
    assert [tier.backend_id for tier in chain] == ["from-file"]


def test_no_chain_anywhere_is_a_refusal_naming_both_places(tmp_path: Path) -> None:
    declared = tmp_path / "declared"
    declared.mkdir()
    with pytest.raises(HelpTiersError) as caught:
        load_help_tiers(_environ(tmp_path, declared))
    message = str(caught.value)
    assert "settings store" in message
    assert "ONEX_SKILL_OVERLAY_ROOTS" in message


def test_a_document_without_a_chain_is_a_refusal_not_a_default(tmp_path: Path) -> None:
    declared = tmp_path / "declared"
    _write(declared, {"schema_version": 1})
    with pytest.raises(HelpTiersError):
        load_help_tiers(_environ(tmp_path, declared))


def test_the_shipped_example_file_loads_as_the_bootstrap_chain(tmp_path: Path) -> None:
    from .conftest import EXAMPLE

    declared = tmp_path / "declared"
    folder = declared / NODE_NAME
    folder.mkdir(parents=True)
    (folder / "overlay.yaml").write_text(EXAMPLE.read_text())
    assert len(load_help_tiers(_environ(tmp_path, declared))) == 8


def test_a_workflow_keeps_the_chain_it_started_with(tmp_path: Path) -> None:
    declared = tmp_path / "declared"
    store = tmp_path / "home" / STORE_RELPATH
    _write(
        store,
        {
            "help_tiers": [
                _tier("t0", "worker-b"),
                _tier("t1", "first-b"),
                _tier("t2", "second-b"),
            ]
        },
    )
    environ = _environ(tmp_path, declared)
    declared.mkdir()
    started_with = load_help_tiers(environ)
    driver = driver_in_help("t0", started_with)
    handler = HandlerLaneWorkHelp()
    first = handler.handle(build_request(driver)).delegate_request
    # the store's chain changes after the workflow started: its first two responders swap
    (store / NODE_NAME / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "help_tiers": [
                    _tier("t0", "worker-b"),
                    _tier("t2", "swapped-b"),
                    _tier("t1", "swapped-c"),
                ]
            }
        )
    )
    assert load_help_tiers(environ)[1].backend_id == "swapped-b"
    driver.send("help_failed", action_id=build_request(driver).intent.action_id)
    second = handler.handle(build_request(driver)).delegate_request
    assert first is not None
    assert second is not None
    assert (first.backend_id, second.backend_id) == ("first-b", "second-b")
