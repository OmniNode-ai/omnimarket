# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The OCC self-bind entry-binding gate, and its wiring (OMN-18304).

A detection tool that is not wired as a pre-merge gate is advisory and gets
ignored, so the wiring is asserted here beside the behaviour — removing either
half is a red test rather than a review catch.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from scripts.ci.check_self_bind_entry_binding import (
    _broken_renderer,
    _check_append_survival,
    _check_end_to_end_binding,
    _check_no_structural_write_path,
    _check_rendered_shape,
    main,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = "scripts/ci/check_self_bind_entry_binding.py"
_HOOK_ID = "occ-self-bind-entry-binding"


def test_gate_passes_on_the_shipped_producer() -> None:
    assert main([]) == 0


def test_positive_control_passes() -> None:
    """The control must find the defect it was built to find.

    ``--self-test`` exits 0 when the broken renderer PRODUCES findings, so a 0
    here means the checks can actually fail.
    """
    assert main(["--self-test"]) == 0


@pytest.mark.parametrize(
    "check",
    [_check_rendered_shape, _check_end_to_end_binding, _check_append_survival],
)
def test_each_binding_check_fires_on_the_pre_fix_renderer(check) -> None:  # type: ignore[no-untyped-def]
    """Per-check positive control: none of the three is vacuous.

    Running them only as a set would let a check that can never fail hide
    behind its two siblings.
    """
    assert check(_broken_renderer), (
        f"{check.__name__} reported clean against a renderer that drops "
        "contract_entry_sha256, so its clean verdict proves nothing"
    )


def test_no_source_file_mints_into_the_retired_structural_tree() -> None:
    assert _check_no_structural_write_path() == []


def test_gate_is_wired_as_a_pre_commit_hook() -> None:
    config = yaml.safe_load((_REPO_ROOT / ".pre-commit-config.yaml").read_text())
    hook_ids = {
        hook.get("id")
        for repo in config.get("repos", [])
        for hook in repo.get("hooks", [])
    }
    assert _HOOK_ID in hook_ids


def test_gate_and_its_positive_control_are_wired_in_ci() -> None:
    ci = (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    runs = re.findall(rf"run: .*{re.escape(_SCRIPT)}[^\n]*", ci)
    assert any(line.endswith("--self-test") for line in runs), (
        "the positive control must run in CI; without it a green gate is "
        "indistinguishable from a gate that cannot fail"
    )
    assert any(not line.endswith("--self-test") for line in runs), (
        "the gate itself must run in CI, not only its positive control"
    )
