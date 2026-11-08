# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Synthetic staged-copy fixtures for the day-one listing tests. No real private root is named."""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.nodes.node_landing_private_reads_effect.models.model_landing_private_reads import (
    ModelLandingPrivateReadsOverlay,
    ModelSourceFile,
)

NODE_DIR = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_landing_private_reads_effect"
)
EXAMPLE_OVERLAY = NODE_DIR / "overlay.example.yaml"
CONTRACT = NODE_DIR / "contract.yaml"

CONTROLLER = "\n".join(
    [
        "import os",
        "# reads example_private_tools for the rule gate (a comment, not a read)",
        'TOOLS = os.environ["EXAMPLE_PRIVATE_TOOLS_HOME"]',
        'GATE = Path(TOOLS) / "example_private_tools" / "rule_gate.yaml"',
        'REPOS = ["ExampleOrg/example_private_tools"]',
        'label = "example_private_tools"',
        'MERGED = "ExampleOrg/x#12 moved example_private_tools/gate.py"',
    ]
)
PLANTED_READ = 'VOCAB = Path("/srv/example-overlays/hygiene.yaml")'


def overlay(**changes: object) -> ModelLandingPrivateReadsOverlay:
    doc = yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8"))
    doc.update(changes)
    return ModelLandingPrivateReadsOverlay.model_validate(doc)


def sources(*, planted: bool = False) -> list[ModelSourceFile]:
    files = [ModelSourceFile(path="scripts/controller.py", text=CONTROLLER)]
    if planted:
        files.append(ModelSourceFile(path="scripts/facts.py", text=PLANTED_READ))
    return files


def write_copy(root: Path, *, planted: bool = False) -> None:
    for source in sources(planted=planted):
        target = root / source.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.text, encoding="utf-8")
    (root / "notes.txt").write_text(PLANTED_READ, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text(PLANTED_READ, encoding="utf-8")


def write_overlay_root(root: Path, **changes: object) -> Path:
    node = root / "node_landing_private_reads_effect"
    node.mkdir(parents=True)
    doc = yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8"))
    doc.update(changes)
    (node / "overlay.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    return root
