# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Enable the lab SLI pack for its existing golden and error chains."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def enable_lab_sli_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    roots = tmp_path / "overlays"
    overlay = roots / "lab_sli_pack" / "overlay.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("status: enabled\n", encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(roots))
