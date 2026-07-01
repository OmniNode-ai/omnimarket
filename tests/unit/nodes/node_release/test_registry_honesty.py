# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Registry-honesty guard for node_release [OMN-13796].

node_release's HandlerRelease is a pure FSM state machine — it performs ZERO
git/gh/PyPI I/O today. The real live-mutating pipeline (bump/pin/PR/merge/tag/
publish) still lives in the un-gated `onex:release` omniclaude skill, not in
this node.

This test mechanically enforces the disposition recorded in contract.yaml and
metadata.yaml so that any future PR that silently adds real I/O to
handler_release.py without updating the registry (or that flips the registry
back to a dishonest "impure compute" / "full_runtime: true" state) fails CI —
per CLAUDE.md Rule 5 (enforcement, not detection).

Never exercise a live prod/PyPI effect in this test.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_release.handlers import handler_release

NODE_DIR = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_release"
)
CONTRACT_PATH = NODE_DIR / "contract.yaml"
METADATA_PATH = NODE_DIR / "metadata.yaml"

# Import names that indicate real external I/O leaking into the "pure FSM"
# handler. Any of these appearing in handler_release.py means the registry
# honesty claim (node_archetype=orchestrator, purity=orchestration,
# full_runtime=false) is stale and must be revisited alongside the code
# change — not silently drifted.
_FORBIDDEN_IO_IMPORTS = (
    "subprocess",
    "requests",
    "httpx",
    "urllib.request",
    "os.system",
    "shutil",
    "git",  # GitPython / any `import git`
)


@pytest.mark.unit
def test_contract_declares_orchestrator_not_compute() -> None:
    """node_release must not self-describe as a pure COMPUTE node (CLAUDE.md
    rule 7a: COMPUTE nodes are pure/no-I/O). It orchestrates FSM phase
    transitions for a pipeline whose actual I/O it does not yet own."""
    contract = yaml.safe_load(CONTRACT_PATH.read_text())

    assert contract["node_type"] == "orchestrator"
    assert contract["descriptor"]["node_archetype"] == "orchestrator"
    assert contract["descriptor"]["purity"] == "orchestration"


@pytest.mark.unit
def test_metadata_full_runtime_is_honestly_false() -> None:
    """`full_runtime: true` would claim the live-mutating release pipeline is
    runnable through this node. It is not — that logic lives in the
    un-gated `onex:release` omniclaude skill. Flag if this ever flips back
    to true without a corresponding real EFFECT implementation."""
    metadata = yaml.safe_load(METADATA_PATH.read_text())

    assert metadata["capabilities"]["full_runtime"] is False
    assert metadata["node_role"] == "orchestrator"


@pytest.mark.unit
def test_metadata_side_effect_class_matches_orchestration_disposition() -> None:
    metadata = yaml.safe_load(METADATA_PATH.read_text())
    assert metadata["capabilities"]["side_effect_class"] == "orchestration"


@pytest.mark.unit
@pytest.mark.parametrize("forbidden", _FORBIDDEN_IO_IMPORTS)
def test_handler_release_has_no_forbidden_io_imports(forbidden: str) -> None:
    """Statically grep handler_release.py's import graph for git/gh/PyPI-style
    I/O. If this ever fails, HandlerRelease has gained real external I/O and
    the registry (contract.yaml archetype/purity, metadata.yaml
    full_runtime) must be updated in the SAME PR — not left stale."""
    source = inspect.getsource(handler_release)
    tree = ast.parse(source)

    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)

    matches = {
        name for name in imported_names if name.split(".")[0] == forbidden.split(".")[0]
    }
    assert not matches, (
        f"handler_release.py imports {matches!r}, which looks like real "
        f"{forbidden!r}-style I/O. Update contract.yaml/metadata.yaml "
        "registry disposition (or move the I/O to a dedicated EFFECT node) "
        "in the same change."
    )


@pytest.mark.unit
def test_handler_release_source_has_no_os_system_or_subprocess_calls() -> None:
    """Belt-and-suspenders: even without an `import subprocess`, a call site
    like `os.system(...)` or `os.popen(...)` would be real shell I/O."""
    source = inspect.getsource(handler_release)
    tree = ast.parse(source)

    forbidden_call_names = {"system", "popen", "spawnv", "spawnl"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in forbidden_call_names
        ):
            offenders.append(node.func.attr)

    assert not offenders, f"handler_release.py contains shell I/O calls: {offenders}"
