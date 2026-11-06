# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22478: the secret-store resolver loads without the removed omnibase_infra path.

omnibase_infra#5683 moved ``ModelInfisicalHandlerConfig`` out of
``omnibase_infra.handlers.models.infisical`` and renamed it
``ModelInfisicalConfig`` in ``omnibase_infra.models.security``. The resolver
imports that model at module scope, and the ledger append loads the resolver, so
the old import stopped every ledger write that took omnibase_infra from a moving
clone. These tests fail on the old import:

* a fresh interpreter that blocks the removed path imports the resolver,
* no module under ``src`` names the removed path or the old class name.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "src"

REMOVED_MODULE = "omnibase_infra.handlers.models.infisical"
REMOVED_CLASS = "ModelInfisicalHandlerConfig"

_BLOCK_AND_IMPORT = f"import sys\nsys.modules[{REMOVED_MODULE!r}] = None\n{{body}}"


def removed_path_uses(source: str) -> list[str]:
    """Each import of the removed module, and each use of its old class name, in ``source``."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            REMOVED_MODULE
        ):
            found.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Import):
            found.extend(
                f"import {alias.name}"
                for alias in node.names
                if alias.name.startswith(REMOVED_MODULE)
            )
        elif isinstance(node, ast.Name) and node.id == REMOVED_CLASS:
            found.append(REMOVED_CLASS)
        elif isinstance(node, ast.alias) and node.name == REMOVED_CLASS:
            found.append(f"import {REMOVED_CLASS}")
    return found


def _run_with_removed_path_blocked(body: str) -> subprocess.CompletedProcess[str]:
    """Run ``body`` in a new interpreter where importing the removed module fails."""
    return subprocess.run(
        [sys.executable, "-c", _BLOCK_AND_IMPORT.format(body=body)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_the_resolver_imports_with_the_removed_infra_path_blocked() -> None:
    result = _run_with_removed_path_blocked(
        "import omnimarket.inference.secret_store_resolver as resolver\n"
        "from omnibase_infra.models.security import ModelInfisicalConfig\n"
        "assert resolver.ModelInfisicalConfig is ModelInfisicalConfig\n"
        "print('loaded')\n"
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "loaded"


def test_no_module_under_src_names_the_removed_infra_path() -> None:
    offenders = {
        str(path.relative_to(REPO_ROOT)): uses
        for path in sorted(SRC_ROOT.rglob("*.py"))
        if (uses := removed_path_uses(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}, (
        f"{REMOVED_MODULE} and {REMOVED_CLASS} left omnibase_infra in #5683; "
        "import ModelInfisicalConfig from omnibase_infra.models.security"
    )


def test_the_old_import_is_named_by_the_scan() -> None:
    """Negative control: the scan and the blocked interpreter both refuse the old import."""
    old = f"from {REMOVED_MODULE} import {REMOVED_CLASS}\nx: {REMOVED_CLASS}\n"
    assert sorted(removed_path_uses(old)) == sorted(
        [
            f"from {REMOVED_MODULE} import ...",
            f"import {REMOVED_CLASS}",
            REMOVED_CLASS,
        ]
    )
    assert (
        removed_path_uses(
            "from omnibase_infra.models.security import ModelInfisicalConfig\n"
        )
        == []
    )
    result = _run_with_removed_path_blocked(old.splitlines()[0] + "\n")
    assert result.returncode != 0
    assert "ImportError" in result.stderr
