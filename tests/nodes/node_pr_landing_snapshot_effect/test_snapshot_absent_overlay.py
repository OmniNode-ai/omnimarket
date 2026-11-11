# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing snapshot step's refusal when its overlay file is absent (OMN-23536).

One pass of the runtime loads <root>/node_pr_landing_snapshot_effect/overlay.yaml
under the roots ONEX_SKILL_OVERLAY_ROOTS names before it takes a tick. When no root
supplies the file, the pass exits 3 and writes one stdout line: a refusal whose
detail names the missing file and the roots searched, not the model's every field.

Golden: the step's passes with a loaded overlay are in test_landing_snapshot.py and
test_snapshot_real_backend.py; this file adds none.
Error: the absent-file refusals above -- no root configured at all, and one empty
root.
Failing control: at the merge base the detail lists all thirteen model fields.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_pr_landing_snapshot_effect.runtime_snapshot import main

pytestmark = pytest.mark.unit


def _refusal(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    """The one stdout line the pass wrote, parsed."""
    [line] = capsys.readouterr().out.splitlines()
    refused: dict[str, object] = json.loads(line)
    return refused


def test_error_with_no_overlay_root_the_refusal_names_the_file_and_no_root(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    code = main([])
    refused = _refusal(capsys)
    assert (code, refused["status"]) == (3, "overlay_refused")
    detail = str(refused["detail"])
    assert "overlay.yaml" in detail
    assert "no root" in detail
    # the refusal names the missing file, not every model field
    assert "node_pr_landing_snapshot_effect.backend" not in detail


def test_error_an_empty_root_is_named_as_the_root_searched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    code = main([])
    refused = _refusal(capsys)
    assert (code, refused["status"]) == (3, "overlay_refused")
    detail = str(refused["detail"])
    assert str(tmp_path) in detail
    assert "overlay.yaml" in detail
    assert "node_pr_landing_snapshot_effect.backend" not in detail
