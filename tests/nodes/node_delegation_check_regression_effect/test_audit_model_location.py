# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shared overlay setting audit row sits in a canonical-shape location (OMN-26652).

The canonical-file-shape ratchet refuses a new code file under omnimarket/events/;
shared models belong under omnimarket/models/.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import omnimarket

_SHARED = "omnimarket.models.overlay_store.model_overlay_setting_audit"


def test_shared_audit_model_lives_under_models() -> None:
    shared = importlib.import_module(_SHARED)
    path = Path(shared.__file__).resolve()
    root = Path(omnimarket.__file__).resolve().parent
    assert path.relative_to(root).parts[0] == "models"


def test_no_new_code_file_in_events_for_the_audit_row() -> None:
    root = Path(omnimarket.__file__).resolve().parent
    assert not (root / "events" / "overlay_setting_audit.py").exists()


def test_both_nodes_use_the_one_class() -> None:
    from omnimarket.nodes.node_delegation_check_regression_effect.handlers import (
        handler_setting_set_trigger,
    )
    from omnimarket.nodes.node_overlay_setting_set_effect.models import (
        model_overlay_setting_audit as node_model,
    )

    shared = importlib.import_module(_SHARED)
    assert (
        handler_setting_set_trigger.ModelOverlaySettingAudit
        is shared.ModelOverlaySettingAudit
    )
    assert node_model.ModelOverlaySettingAudit is shared.ModelOverlaySettingAudit
