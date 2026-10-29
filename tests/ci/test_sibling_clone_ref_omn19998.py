# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-19998 - CI clones a sibling at the ref uv.lock pins, not at ``v<version>``.

A ``uv.lock`` git source pinned to a lab dev tag locks a ``version`` that has no
``v<version>`` release tag, so ``git clone --branch v<version>`` died with exit
128 ("Remote branch v... not found") in every job that clones the sibling. The
clone ref expression the workflows run prints the pinned tag for a tag pin and
``v<version>`` for any other entry. The tests run that expression, taken from
``ci.yml``, against fixture lock files.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
DEV_TAG = "dev-20261028111658-97c5d1c2802a"
LOCK = f"""\
version = 1

[[package]]
name = "omnibase-core"
version = "0.47.58"
source = {{ git = "https://github.com/OmniNode-ai/omnibase_core.git?tag={DEV_TAG}#97c5d1c2802acbdd13940c1a4ab1b07cf4fb7418" }}

[[package]]
name = "omnibase-compat"
version = "0.5.7"
source = {{ registry = "https://pypi.org/simple" }}

[[package]]
name = "omnibase-infra"
version = "0.38.77"
source = {{ git = "https://github.com/OmniNode-ai/omnibase_infra.git?rev=3540659c6633#3540659c663324b4c9ac711f8888f2cb9fc8ea52" }}
"""
_REF_EXPR = re.compile(r"""\$\(awk -v p=omnibase-core '[^'\n]*' uv\.lock\)""")


def _ref_expression() -> str:
    match = _REF_EXPR.search((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    assert match, "ci.yml carries no lock-derived clone ref for omnibase-core"
    return match.group(0)


def _clone_ref(package: str, tmp_path: Path) -> str:
    (tmp_path / "uv.lock").write_text(LOCK, encoding="utf-8")
    expression = _ref_expression().replace("p=omnibase-core", f"p={package}")
    done = subprocess.run(
        ["bash", "-c", f"printf %s {expression}"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def test_git_tag_pin_clones_the_pinned_tag(tmp_path: Path) -> None:
    assert _clone_ref("omnibase-core", tmp_path) == DEV_TAG


def test_registry_release_clones_its_version_tag(tmp_path: Path) -> None:
    assert _clone_ref("omnibase-compat", tmp_path) == "v0.5.7"


def test_git_source_without_a_tag_clones_its_version_tag(tmp_path: Path) -> None:
    assert _clone_ref("omnibase-infra", tmp_path) == "v0.38.77"


def test_absent_package_prints_no_ref(tmp_path: Path) -> None:
    assert _clone_ref("omninode-memory", tmp_path) == ""


def test_no_workflow_derives_the_clone_ref_from_the_lock_version() -> None:
    offenders = [
        f"{path.name}:{number}"
        for path in sorted(WORKFLOWS.glob("*.yml"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "uv.lock" in line and "sed -n 's/^version" in line
    ]
    assert offenders == []
