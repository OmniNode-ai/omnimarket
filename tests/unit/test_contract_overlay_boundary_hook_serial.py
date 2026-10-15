# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract-overlay-boundary hook runs as one serial process (OMN-20201).

The hook passes the staged node contracts to
scripts/validation/validate_contract_overlay_boundary.py, which aggregates its
exit code over the whole list, so it declares require_serial: true like every
other filename hook under the OMN-20241 policy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_HOOK_ID = "contract-overlay-boundary"


def _hook(hook_id: str) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[2]
    config = yaml.safe_load(
        (repo_root / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    )
    matches = [
        hook
        for repo in config["repos"]
        for hook in repo["hooks"]
        if hook.get("id") == hook_id
    ]
    assert len(matches) == 1, f"expected one {hook_id} hook, found {len(matches)}"
    hook: dict[str, Any] = matches[0]
    return hook


@pytest.mark.unit
def test_contract_overlay_boundary_hook_declares_require_serial() -> None:
    hook = _hook(_HOOK_ID)
    assert hook.get("pass_filenames") is True
    assert hook.get("require_serial") is True, (
        f"{_HOOK_ID} must declare require_serial: true (OMN-20241)"
    )
