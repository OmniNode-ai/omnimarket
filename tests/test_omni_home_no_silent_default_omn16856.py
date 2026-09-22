# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMNI_HOME has no silent default anywhere in shipped source (OMN-16856).

ARCH-002 (``no_silent_fallback``) names ``os.environ.get("OMNI_HOME",
"/some/default")`` as its canonical anti-pattern, but its checker runs only when
the architectural-invariant loop is invoked, so nothing stopped the shape from
landing. It did land: ``handler_post_merge_sync.py`` defaulted to
``Path.home() / "Code" / "omni_home"`` — an operator-machine layout baked into
the wheel — until OMN-17459. ARCH-002's regex would not have caught it either,
because the default was a computed expression rather than a string literal.

This test is the enforcing surface for the OMNI_HOME half of ARCH-002. It walks
the AST of every module under ``src/`` and refuses:

* ``os.environ.get("OMNI_HOME", <default>)`` / ``os.getenv("OMNI_HOME",
  <default>)`` where the default is anything other than ``""`` or ``None`` —
  literal or computed. An empty default is left to the call site, whose
  emptiness check is what fails closed.
* any ``Path.home() / "Code" / "omni_home"`` chain.

Both directions are proven against synthetic sources, including the exact
pre-OMN-17459 line, so a scan that silently matches nothing cannot pass.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "omnimarket"
_VAR = "OMNI_HOME"
# Every module the scan must at least see; a collapsed walk fails the floor.
_MIN_MODULES_SCANNED = 500


def _is_env_read(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "get":
        target = func.value
        return (
            isinstance(target, ast.Attribute)
            and target.attr == "environ"
            and isinstance(target.value, ast.Name)
            and target.value.id == "os"
        )
    if isinstance(func, ast.Attribute) and func.attr == "getenv":
        return isinstance(func.value, ast.Name) and func.value.id == "os"
    return False


def _is_allowed_default(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value in ("", None)


def _is_home_layout(node: ast.BinOp) -> bool:
    """Match ``Path.home() / "Code" / "omni_home"`` (left-nested BinOp chain)."""
    parts: list[ast.expr] = []
    cur: ast.expr = node
    while isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Div):
        parts.append(cur.right)
        cur = cur.left
    parts.append(cur)
    parts.reverse()
    head = parts[0]
    is_home = (
        isinstance(head, ast.Call)
        and isinstance(head.func, ast.Attribute)
        and head.func.attr == "home"
    )
    literals = [p.value for p in parts[1:] if isinstance(p, ast.Constant)]
    return is_home and "omni_home" in literals


def find_violations(source: str, label: str) -> list[str]:
    findings: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and _is_env_read(node) and node.args:
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and first.value == _VAR):
                continue
            default = node.args[1] if len(node.args) > 1 else None
            if default is not None and not _is_allowed_default(default):
                findings.append(
                    f"{label}:{node.lineno}: {_VAR} read with a silent default "
                    f"{ast.unparse(default)!r}"
                )
        elif isinstance(node, ast.BinOp) and _is_home_layout(node):
            findings.append(
                f"{label}:{node.lineno}: hardcoded home-directory layout "
                f"{ast.unparse(node)!r}"
            )
    return findings


@pytest.mark.unit
class TestDetectorPositiveControls:
    def test_pre_omn17459_line_is_refused(self) -> None:
        source = (
            "import os\nfrom pathlib import Path\n"
            'x = os.environ.get("OMNI_HOME", str(Path.home() / "Code" / "omni_home"))\n'
        )
        findings = find_violations(source, "fixture.py")
        assert len(findings) == 2, findings

    def test_literal_default_is_refused(self) -> None:
        source = 'import os\nx = os.environ.get("OMNI_HOME", "/some/default")\n'
        assert find_violations(source, "fixture.py")

    def test_getenv_default_is_refused(self) -> None:
        source = 'import os\nx = os.getenv("OMNI_HOME", "/opt/omni")\n'
        assert find_violations(source, "fixture.py")

    @pytest.mark.parametrize(
        "line",
        [
            'x = os.environ["OMNI_HOME"]',
            'x = os.environ.get("OMNI_HOME")',
            'x = os.environ.get("OMNI_HOME", "")',
            'x = os.environ.get("OMNI_HOME", None)',
            'x = os.environ.get("OTHER_VAR", "/some/default")',
        ],
    )
    def test_fail_fast_shapes_pass(self, line: str) -> None:
        assert find_violations(f"import os\n{line}\n", "fixture.py") == []


@pytest.mark.unit
def test_shipped_source_has_no_silent_omni_home_default() -> None:
    modules = sorted(_SRC_ROOT.rglob("*.py"))
    assert len(modules) >= _MIN_MODULES_SCANNED, (
        f"scan saw {len(modules)} modules under {_SRC_ROOT}; the walk collapsed"
    )
    findings: list[str] = []
    for path in modules:
        rel = str(path.relative_to(_SRC_ROOT.parent))
        findings.extend(find_violations(path.read_text(encoding="utf-8"), rel))
    assert findings == [], "\n".join(findings)
