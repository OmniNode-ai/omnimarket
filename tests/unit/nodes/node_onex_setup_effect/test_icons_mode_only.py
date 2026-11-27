# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC6: one setting picks the icons, ``mode``.

Failure modes:
- IC1: the design file carries a second switch (``icons.enabled``) that nothing reads, so setting it to false still shows Nerd Font glyphs with mode nerd.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import (
    ModelTuiIcons,
    tui_design,
)

pytestmark = pytest.mark.unit


def test_a_second_icon_switch_is_refused() -> None:
    """IC1: ``enabled`` is not a setting; a file that sets it is refused."""
    with pytest.raises(ValidationError):
        ModelTuiIcons.model_validate({"mode": "nerd", "enabled": False})
    assert "enabled" not in tui_design().icons.model_dump()
