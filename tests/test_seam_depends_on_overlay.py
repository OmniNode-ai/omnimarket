# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams of the depends-on overlay gate (OMN-24537): contract to overlay, and ticket to prerequisite.

Contract to overlay. A node contract declares an overlay row under ``metadata.declared_dependencies``; the omnibase_core
validator judges it; this tree's node supplies the overlay facts; the CI step names the layout. Each hand-off is read from
BOTH sides here, so a change to the kind name, the declaration key, the overlay file layout or the CI step's flags fails
this file, not the gate in the dark.

Ticket to prerequisite. A ticket contract declares a sibling ticket under ``context.declared_dependencies``; the lab fill
generator (not built here) would supply the facts. The seam this tree holds is that the validator reads a ticket contract
of the shape the tree's ticket-contract renderer writes, and that this consumer supplies no facts for a ticket kind, so a
ticket dependency declared on a node contract is refused rather than passed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from omnibase_core.models.depends_on.model_depends_on_check_input import (
    ModelDependsOnCheckInput,
)
from omnibase_core.models.depends_on.model_depends_on_facts import ModelDependsOnFacts
from omnibase_core.models.depends_on.model_depends_on_rules import ModelDependsOnRules
from omnibase_core.validation.checker_depends_on import check_depends_on
from omnibase_core.validation.validator_depends_on import ValidatorDependsOn

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_FILE_NAME,
    OVERLAY_ROOTS_ENV,
    find_node_overlay,
)
from omnimarket.nodes.node_depends_on_overlay_check_effect.handlers.handler_depends_on_overlay_check import (
    HandlerDependsOnOverlayCheck,
)
from omnimarket.nodes.node_depends_on_overlay_check_effect.models import (
    EnumDependsOnFindingCode,
    ModelDependsOnOverlayCheckCommand,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
NODES_ROOT = REPO_ROOT / "src" / "omnimarket" / "nodes"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
NODE_NAME = "node_depends_on_overlay_check_effect"


def _rules() -> ModelDependsOnRules:
    return ModelDependsOnRules.from_contract(ValidatorDependsOn().contract)


def _ci_flags() -> dict[str, str]:
    """The flags of this node's command in the CI workflow, read as the workflow writes them."""
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    start = text.index(f"python -m omnimarket.nodes.{NODE_NAME}")
    block = text[start : start + 600]
    return dict(re.findall(r"--([a-z-]+) ([^\s\\]+)", block))


def test_seam_ci_overlay_kind_is_a_kind_the_core_validator_declares() -> None:
    """The kind the CI step supplies facts under is one the validator's contract lists; a rename on either side fails."""
    kinds = _rules().row("dependency_kind_unknown").lists["kinds"]
    assert _ci_flags()["overlay-kind"] in kinds


def test_seam_ci_overlay_file_is_the_file_the_runtime_reader_loads() -> None:
    """The overlay file name in CI is the reader's ``OVERLAY_FILE_NAME``: the gate and the runtime read one layout."""
    assert _ci_flags()["overlay-file"] == OVERLAY_FILE_NAME


def test_seam_ci_contract_file_is_the_file_every_node_ships() -> None:
    name = _ci_flags()["contract-file"]
    assert (NODES_ROOT / "node_eval_tracking_schedule_compute" / name).is_file()


def test_seam_facts_agree_with_the_runtime_reader_layout(tmp_path: Path) -> None:
    """For the same overlays root the gate's facts and ``find_node_overlay`` (the runtime's lookup) agree on each id."""
    root = tmp_path / "overlays"
    for present in ("node_a", "node_b"):
        (root / present).mkdir(parents=True)
        (root / present / OVERLAY_FILE_NAME).write_text("limits: {}\n", "utf-8")
    (root / "node_c").mkdir()  # a directory with no document is no row
    nodes = tmp_path / "nodes"
    for node in ("node_a", "node_b", "node_c"):
        (nodes / node).mkdir(parents=True)
        (nodes / node / "contract.yaml").write_text(
            yaml.safe_dump(
                {
                    "metadata": {
                        "declared_dependencies": [{"kind": "overlay_key", "name": node}]
                    }
                }
            ),
            "utf-8",
        )
    result = HandlerDependsOnOverlayCheck().handle(
        ModelDependsOnOverlayCheckCommand(
            nodes_root=str(nodes),
            overlays_root=str(root),
            contract_file_name="contract.yaml",
            overlay_file_name=OVERLAY_FILE_NAME,
            overlay_kind="overlay_key",
        )
    )
    refused = {finding.node for finding in result.findings}
    reader = {
        node
        for node in ("node_a", "node_b", "node_c")
        if find_node_overlay(node, {OVERLAY_ROOTS_ENV: str(root)}) is None
    }
    assert refused == reader == {"node_c"}


def test_seam_every_declaration_in_the_tree_is_well_formed_and_of_a_declared_kind(
    tmp_path: Path,
) -> None:
    """No contract in the tree declares a malformed entry or an unknown kind; an overlay row for each makes the tree pass."""
    rules = ModelDependsOnRules.from_contract(ValidatorDependsOn().contract)
    keys = rules.row("dependency_absent").lists["declaration_keys"]
    wanted: dict[str, list[str]] = {}
    for contract in sorted(NODES_ROOT.glob("*/contract.yaml")):
        document = yaml.safe_load(contract.read_text(encoding="utf-8")) or {}
        for key in keys:
            node: object = document
            for part in key.split("."):
                node = node.get(part) if isinstance(node, dict) else None
            for entry in node if isinstance(node, list) else []:
                wanted.setdefault(contract.parent.name, []).append(entry["name"])
    assert len(wanted) >= 2, "the tree declares no overlay dependency to judge"
    root = tmp_path / "overlays"
    for names in wanted.values():
        for name in names:
            (root / name).mkdir(parents=True, exist_ok=True)
            (root / name / OVERLAY_FILE_NAME).write_text("a: 1\n", "utf-8")
    result = HandlerDependsOnOverlayCheck().handle(
        ModelDependsOnOverlayCheckCommand(
            nodes_root=str(NODES_ROOT),
            overlays_root=str(root),
            contract_file_name="contract.yaml",
            overlay_file_name=OVERLAY_FILE_NAME,
            overlay_kind="overlay_key",
        )
    )
    assert result.findings == ()
    assert result.nodes_checked == len(wanted)


def test_seam_no_node_contract_reads_the_old_depends_on_key_as_a_declaration() -> None:
    """The measured collision: a node contract keeps its own ``metadata.depends_on`` (node names) and is not judged."""
    contract = NODES_ROOT / "node_pipeline_fill" / "contract.yaml"
    document = yaml.safe_load(contract.read_text(encoding="utf-8"))
    assert isinstance(document["metadata"].get("depends_on"), list)
    result = check_depends_on(
        ModelDependsOnCheckInput(
            source_name="node_pipeline_fill/contract.yaml",
            text=contract.read_text(encoding="utf-8"),
            facts=ModelDependsOnFacts(present={"overlay_key": ()}),
            contract=ValidatorDependsOn().contract,
        )
    )
    assert result.issues == ()
    assert result.dependencies_checked == 0


def test_seam_ticket_dependency_on_a_node_contract_is_refused_not_passed() -> None:
    """This consumer supplies overlay facts only; a sibling-ticket declaration reads FACTS_UNSUPPLIED, never present."""
    result = check_depends_on(
        ModelDependsOnCheckInput(
            source_name="contracts/OMN-1.yaml",
            text=yaml.safe_dump(
                {
                    "context": {
                        "declared_dependencies": [
                            {"kind": "sibling_ticket", "name": "OMN-2"}
                        ]
                    }
                }
            ),
            facts=ModelDependsOnFacts(present={"overlay_key": ()}),
            contract=ValidatorDependsOn().contract,
        )
    )
    assert [issue.code for issue in result.issues] == [
        EnumDependsOnFindingCode.FACTS_UNSUPPLIED.value
    ]


def test_seam_failing_control_a_renamed_kind_is_seen_as_unsupplied(
    tmp_path: Path,
) -> None:
    """If the CI step supplied the facts under another kind name, every declaration would read FACTS_UNSUPPLIED."""
    nodes = tmp_path / "nodes" / "node_a"
    nodes.mkdir(parents=True)
    (nodes / "contract.yaml").write_text(
        yaml.safe_dump(
            {
                "metadata": {
                    "declared_dependencies": [{"kind": "overlay_key", "name": "node_a"}]
                }
            }
        ),
        "utf-8",
    )
    root = tmp_path / "overlays" / "node_a"
    root.mkdir(parents=True)
    (root / OVERLAY_FILE_NAME).write_text("a: 1\n", "utf-8")
    result = HandlerDependsOnOverlayCheck().handle(
        ModelDependsOnOverlayCheckCommand(
            nodes_root=str(tmp_path / "nodes"),
            overlays_root=str(tmp_path / "overlays"),
            contract_file_name="contract.yaml",
            overlay_file_name=OVERLAY_FILE_NAME,
            overlay_kind="overlay_keys",
        )
    )
    assert [f.code for f in result.findings] == [
        EnumDependsOnFindingCode.FACTS_UNSUPPLIED
    ]
