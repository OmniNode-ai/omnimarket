# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""omnimarket must not depend on onex_change_control at runtime.

Operator ruling: product code does not depend on the governance repo. The
practical consequence is that omnimarket must be installable from the public
index by someone who has never heard of onex_change_control, which was not true
before: `onex-change-control` has never been published to PyPI, so the pin was
satisfiable only from a local workspace clone via a `[tool.uv.sources]` git
override. Published wheel metadata carries the plain pin and the override does
not travel with it, so every published omnimarket version was unresolvable and
the failure surfaced three packages away from its cause.

That failure mode is invisible from a developer checkout — the override makes it
work locally — so a test is the only place it can be caught early. These are
static source and metadata checks: they hold on a machine that happens to have
onex_change_control installed, which a runtime import probe would not.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"

#: Matches a real import statement, not a mention in a docstring, comment, or a
#: URL. Several modules legitimately name the onex_change_control *repository*
#: in prose — e.g. nodes that fetch a governed file from that repo over HTTPS —
#: and those are not dependencies of this package.
_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+onex_change_control\b", re.MULTILINE)


@pytest.mark.unit
def test_no_source_file_imports_onex_change_control() -> None:
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        for match in _IMPORT_RE.finditer(path.read_text(encoding="utf-8")):
            line_no = path.read_text(encoding="utf-8")[: match.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(_REPO_ROOT).as_posix()}:{line_no}")
    assert not offenders, (
        "omnimarket source imports onex_change_control at "
        f"{offenders}. Product code must not depend on the governance repo. "
        "Shared data types belong in omnibase_core (see "
        "omnibase_core.models.overseer / omnibase_core.enums.overseer); "
        "governance-side logic belongs in onex_change_control and should not be "
        "called from here. Do not re-add the import behind a try/except "
        "ImportError fallback — a silent degrade is what hid this last time."
    )


@pytest.mark.unit
def test_pyproject_declares_no_onex_change_control_dependency() -> None:
    """The pin and its uv git source must both be absent.

    Checked separately from the import scan because either one alone reintroduces
    the unresolvable-from-PyPI failure: the dependency pin makes the wheel
    unresolvable, and the `[tool.uv.sources]` git override is what would hide it
    again from everyone working in the workspace.
    """
    raw = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    data = tomllib.loads(raw)

    declared = [
        dep
        for dep in data.get("project", {}).get("dependencies", [])
        if "onex-change-control" in dep or "onex_change_control" in dep
    ]
    assert not declared, (
        f"pyproject.toml [project].dependencies declares {declared}. "
        "onex-change-control is not published to PyPI, so this pin makes every "
        "released omnimarket wheel unresolvable for anyone installing from the "
        "public index."
    )

    sources = data.get("tool", {}).get("uv", {}).get("sources", {})
    assert "onex-change-control" not in sources, (
        "pyproject.toml [tool.uv.sources] pins onex-change-control to a git rev. "
        "That override makes the dependency resolve inside this workspace while "
        "leaving published wheels broken — the exact asymmetry that hid this."
    )
