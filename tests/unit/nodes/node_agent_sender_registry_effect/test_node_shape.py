# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node's command line entry and class names meet the canonical-shape and lifecycle-class ratchets."""

from __future__ import annotations

import ast
from pathlib import Path

NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_agent_sender_registry_effect"
)
BARRED_WORDS = ("Registry", "Service", "Manager", "Controller", "Server", "Daemon")


def test_command_line_entry_is_a_node_runtime_beside_the_contract() -> None:
    assert (NODE / "contract.yaml").is_file()
    assert not (NODE / "__main__.py").exists()
    runtimes = sorted(NODE.glob("runtime_*.py"))
    assert [p.name for p in runtimes] == ["runtime_agent_sender_registry.py"]


def test_no_class_in_the_node_carries_a_barred_type_word() -> None:
    offenders = [
        f"{path.relative_to(NODE)}:{cls.name}"
        for path in NODE.rglob("*.py")
        for cls in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(cls, ast.ClassDef)
        and cls.name.startswith("Secret")
        and any(word in cls.name for word in BARRED_WORDS)
    ]
    assert offenders == []
