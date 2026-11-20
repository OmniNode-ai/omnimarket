# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The depends-on overlay check node (OMN-24537): golden passes, the recorded incident as the failing control, the error chain, the CLI.

The incident the control replays: the weekly coding test trigger (``node_eval_tracking_schedule_compute``) loads its overlay on
the first runtime tick; merged before its overlay row existed, it raised ``NodeOverlayNotFoundError`` on every tick. The real
contract of that node, with the dependency it now declares, must be refused when the overlays root has no row for it and
passed when it has one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

import omnimarket.nodes.node_eval_tracking_schedule_compute as eval_tracking_package
from omnimarket.nodes.node_depends_on_overlay_check_effect.handlers.handler_depends_on_overlay_check import (
    HandlerDependsOnOverlayCheck,
)
from omnimarket.nodes.node_depends_on_overlay_check_effect.models import (
    EnumDependsOnFindingCode,
    ModelDependsOnOverlayCheckCommand,
)
from omnimarket.nodes.node_depends_on_overlay_check_effect.runtime_depends_on_overlay_check import (
    main,
)

pytestmark = pytest.mark.unit

EVAL_TRACKING = "node_eval_tracking_schedule_compute"
CONTRACT_FILE = "contract.yaml"
OVERLAY_FILE = "overlay.yaml"
KIND = "overlay_key"


def _real_contract() -> dict[str, Any]:
    """The real contract of the weekly coding test trigger, with the overlay dependency it now declares."""
    path = Path(eval_tracking_package.__file__).parent / CONTRACT_FILE
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document.setdefault("metadata", {})["declared_dependencies"] = [
        {"kind": KIND, "name": EVAL_TRACKING}
    ]
    return document


def _write_nodes(tmp_path: Path, contracts: dict[str, dict[str, Any]]) -> Path:
    root = tmp_path / "nodes"
    for node, document in contracts.items():
        (root / node).mkdir(parents=True)
        (root / node / CONTRACT_FILE).write_text(yaml.safe_dump(document), "utf-8")
    return root


def _write_overlays(tmp_path: Path, documents: dict[str, dict[str, Any]]) -> Path:
    root = tmp_path / "overlays"
    root.mkdir(exist_ok=True)
    for overlay_id, document in documents.items():
        (root / overlay_id).mkdir(parents=True, exist_ok=True)
        (root / overlay_id / OVERLAY_FILE).write_text(yaml.safe_dump(document), "utf-8")
    return root


def _run(nodes: Path, overlays: Path, node_names: tuple[str, ...] = ()) -> Any:
    return HandlerDependsOnOverlayCheck().handle(
        ModelDependsOnOverlayCheckCommand(
            nodes_root=str(nodes),
            overlays_root=str(overlays),
            contract_file_name=CONTRACT_FILE,
            overlay_file_name=OVERLAY_FILE,
            overlay_kind=KIND,
            node_names=node_names,
        )
    )


def _dependency(name: str, field: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"kind": KIND, "name": name}
    if field is not None:
        entry["field"] = field
    return {"metadata": {"declared_dependencies": [entry]}}


class TestGolden:
    def test_golden_eval_tracking_with_its_overlay_row_passes(
        self, tmp_path: Path
    ) -> None:
        nodes = _write_nodes(tmp_path, {EVAL_TRACKING: _real_contract()})
        overlays = _write_overlays(tmp_path, {EVAL_TRACKING: {"local_days": 7}})
        result = _run(nodes, overlays)
        assert result.ok
        assert result.nodes_checked == 1
        assert result.dependencies_checked == 1
        assert result.findings == ()

    def test_golden_overlay_key_with_field_passes(self, tmp_path: Path) -> None:
        nodes = _write_nodes(tmp_path, {"node_x": _dependency("node_x", "limits")})
        overlays = _write_overlays(tmp_path, {"node_x": {"limits": {"a": 1}}})
        assert _run(nodes, overlays).ok

    def test_golden_node_that_declares_nothing_is_not_judged(
        self, tmp_path: Path
    ) -> None:
        nodes = _write_nodes(tmp_path, {"node_y": {"name": "node_y"}})
        overlays = _write_overlays(tmp_path, {"other": {"a": 1}})
        result = _run(nodes, overlays)
        assert result.ok
        assert result.nodes_checked == 0
        assert result.overlays_present == 1


class TestFailingControl:
    def test_control_eval_tracking_without_its_overlay_row_is_refused(
        self, tmp_path: Path
    ) -> None:
        """The incident: the overlays root has other rows but none for the weekly coding test trigger."""
        nodes = _write_nodes(tmp_path, {EVAL_TRACKING: _real_contract()})
        overlays = _write_overlays(tmp_path, {"node_some_other": {"a": 1}})
        result = _run(nodes, overlays)
        assert not result.ok
        [finding] = result.findings
        assert finding.code == EnumDependsOnFindingCode.DEPENDENCY_ABSENT
        assert finding.node == EVAL_TRACKING
        assert EVAL_TRACKING in finding.detail

    def test_control_eval_tracking_same_contract_passes_once_the_row_exists(
        self, tmp_path: Path
    ) -> None:
        """The control's other half: nothing but the overlay row differs between the refusal and the pass."""
        nodes = _write_nodes(tmp_path, {EVAL_TRACKING: _real_contract()})
        refused = _run(nodes, _write_overlays(tmp_path, {"node_some_other": {"a": 1}}))
        passed = _run(nodes, _write_overlays(tmp_path, {EVAL_TRACKING: {"a": 1}}))
        assert (refused.ok, passed.ok) == (False, True)

    def test_control_field_missing_from_the_overlay_document_is_refused(
        self, tmp_path: Path
    ) -> None:
        nodes = _write_nodes(tmp_path, {"node_x": _dependency("node_x", "limits")})
        overlays = _write_overlays(tmp_path, {"node_x": {"other_key": 1}})
        [finding] = _run(nodes, overlays).findings
        assert finding.code == EnumDependsOnFindingCode.DEPENDENCY_ABSENT
        assert "node_x:limits" in finding.detail


class TestErrorChain:
    def test_malformed_declaration_is_refused(self, tmp_path: Path) -> None:
        contract = {"metadata": {"declared_dependencies": [{"kind": KIND}]}}
        nodes = _write_nodes(tmp_path, {"node_x": contract})
        [finding] = _run(nodes, _write_overlays(tmp_path, {"a": {"a": 1}})).findings
        assert finding.code == EnumDependsOnFindingCode.DEPENDENCY_MALFORMED

    def test_unknown_kind_is_refused(self, tmp_path: Path) -> None:
        contract = {
            "metadata": {
                "declared_dependencies": [{"kind": "moon_phase", "name": "full"}]
            }
        }
        nodes = _write_nodes(tmp_path, {"node_x": contract})
        [finding] = _run(nodes, _write_overlays(tmp_path, {"a": {"a": 1}})).findings
        assert finding.code == EnumDependsOnFindingCode.DEPENDENCY_KIND_UNKNOWN

    def test_other_kind_is_refused_because_this_consumer_supplies_no_facts_for_it(
        self, tmp_path: Path
    ) -> None:
        contract = {
            "metadata": {
                "declared_dependencies": [{"kind": "sibling_ticket", "name": "OMN-1"}]
            }
        }
        nodes = _write_nodes(tmp_path, {"node_x": contract})
        [finding] = _run(nodes, _write_overlays(tmp_path, {"a": {"a": 1}})).findings
        assert finding.code == EnumDependsOnFindingCode.FACTS_UNSUPPLIED

    def test_existing_depends_on_key_of_another_shape_is_not_read(
        self, tmp_path: Path
    ) -> None:
        contract = {"metadata": {"depends_on": ["node_a", "node_b"]}}
        nodes = _write_nodes(tmp_path, {"node_x": contract})
        result = _run(nodes, _write_overlays(tmp_path, {"a": {"a": 1}}))
        assert result.ok
        assert result.nodes_checked == 0

    def test_node_names_scope_judges_only_the_named_nodes(self, tmp_path: Path) -> None:
        nodes = _write_nodes(
            tmp_path,
            {"good": {"name": "good"}, "bad": _dependency("node_missing")},
        )
        overlays = _write_overlays(tmp_path, {"a": {"a": 1}})
        assert _run(nodes, overlays, node_names=("good",)).ok
        assert not _run(nodes, overlays).ok


class TestCli:
    @staticmethod
    def _argv(nodes: Path, overlays: Path) -> list[str]:
        return [
            "--nodes-root",
            str(nodes),
            "--overlays-root",
            str(overlays),
            "--contract-file",
            CONTRACT_FILE,
            "--overlay-file",
            OVERLAY_FILE,
            "--overlay-kind",
            KIND,
        ]

    def test_cli_exit_0_when_present(self, tmp_path: Path) -> None:
        nodes = _write_nodes(tmp_path, {EVAL_TRACKING: _real_contract()})
        overlays = _write_overlays(tmp_path, {EVAL_TRACKING: {"a": 1}})
        assert main(self._argv(nodes, overlays)) == 0

    def test_cli_exit_1_and_names_the_node_when_absent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        nodes = _write_nodes(tmp_path, {EVAL_TRACKING: _real_contract()})
        overlays = _write_overlays(tmp_path, {"node_some_other": {"a": 1}})
        assert main(self._argv(nodes, overlays)) == 1
        assert f"refused {EVAL_TRACKING} dependency_absent" in capsys.readouterr().out

    def test_cli_exit_3_when_a_root_cannot_be_read(self, tmp_path: Path) -> None:
        nodes = _write_nodes(tmp_path, {EVAL_TRACKING: _real_contract()})
        assert main(self._argv(nodes, tmp_path / "absent")) == 3
