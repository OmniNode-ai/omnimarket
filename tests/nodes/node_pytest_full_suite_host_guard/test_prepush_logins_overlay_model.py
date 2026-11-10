# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The prepush logins overlay model, and the guard reading the same bytes (OMN-23597)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_pytest_full_suite_host_guard.handlers.handler_pytest_full_suite_host_guard import (
    login_overlay_block,
    parse_login_rows,
)
from omnimarket.nodes.node_pytest_full_suite_host_guard.models import (
    model_prepush_logins_overlay,
)
from omnimarket.nodes.node_pytest_full_suite_host_guard.models.model_prepush_logins_overlay import (
    ModelPrepushLoginsOverlay,
)

pytestmark = pytest.mark.unit

TABLE = (
    "# label login uv_path run_folder\nh051 second-login .local/bin/uv onex-prepush\n"
)
DOCUMENT = {"schema_version": 1, "table": TABLE}


def ENV(tmp_path: Path) -> dict[str, str]:  # noqa: N802 -- reads as the environment mapping
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def _write_overlay(tmp_path: Path, document: dict[str, object]) -> None:
    (tmp_path / "prepush_logins").mkdir()
    (tmp_path / "prepush_logins" / "overlay.yaml").write_text(
        yaml.safe_dump(document), encoding="utf-8"
    )


def test_golden_overlay_loads_the_way_the_reader_loads_it(tmp_path: Path) -> None:
    _write_overlay(tmp_path, DOCUMENT)
    overlay = load_structured_overlay(
        "prepush_logins", ModelPrepushLoginsOverlay, ENV(tmp_path)
    )
    assert parse_login_rows(overlay.table) == {
        ("h051", "second-login"): (".local/bin/uv", "onex-prepush")
    }


def test_golden_seam_pins_the_model_and_the_guard_on_the_same_bytes() -> None:
    raw = (
        "schema_version: 1\ntable: |\n  # label login uv_path run_folder\n"
        "  h051 second-login .local/bin/uv onex-prepush\n"
    )
    overlay = ModelPrepushLoginsOverlay.model_validate(yaml.safe_load(raw))
    assert parse_login_rows(login_overlay_block(raw)) == parse_login_rows(overlay.table)
    assert parse_login_rows(overlay.table)


@pytest.mark.parametrize(
    "document",
    [
        {"schema_version": 1, "table": "h051 second-login ~/uv onex-prepush"},
        {"schema_version": 1, "table": "h051 second-login uv ../x"},
        {"schema_version": 1, "table": "h051 second-login uv"},
        {
            "schema_version": 1,
            "table": "h051 second-login uv onex-prepush\nh051 second-login uv onex-prepush",
        },
        {"schema_version": 1, "table": "# comment only, no rows"},
        {
            "schema_version": 2,
            "table": "h051 second-login .local/bin/uv onex-prepush",
        },
        {
            "schema_version": 1,
            "table": "h051 second-login .local/bin/uv onex-prepush",
            "other": 1,
        },
    ],
)
def test_a_wrong_login_overlay_refuses(
    document: dict[str, object], tmp_path: Path
) -> None:
    _write_overlay(tmp_path, document)
    with pytest.raises(NodeOverlayError):
        load_structured_overlay(
            "prepush_logins", ModelPrepushLoginsOverlay, ENV(tmp_path)
        )


def test_injected_break_tilde_document_loads_when_the_node_rule_is_bypassed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        model_prepush_logins_overlay,
        "parse_login_rows",
        lambda _block: {("h051", "second-login"): ("a", "b")},
    )
    _write_overlay(
        tmp_path, {"schema_version": 1, "table": "h051 second-login ~/uv onex-prepush"}
    )
    overlay = load_structured_overlay(
        "prepush_logins", ModelPrepushLoginsOverlay, ENV(tmp_path)
    )
    assert overlay.table == "h051 second-login ~/uv onex-prepush"
