# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The install request effect overlay's two bus settings (OMN-24546).

The publisher login variable and the owed reading's quiet window live only in
the overlay, as every setting of this node does: the packaged example declares
both, an overlay that loses either -- or declares a blank login variable or a
quiet window below one second -- refuses the load and names the key, and the
loader finds the overlay under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_install_request_effect import overlay as overlay_module
from omnimarket.nodes.node_install_request_effect.overlay import (
    NODE_NAME,
    ModelInstallRequestEffectOverlay,
    load_overlay,
)

pytestmark = pytest.mark.unit

_EXAMPLE = Path(overlay_module.__file__).parent / "overlay.example.yaml"


def _example_document() -> dict[str, object]:
    return yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))


def test_the_example_overlay_declares_the_publisher_login_and_the_quiet_window() -> (
    None
):
    overlay = ModelInstallRequestEffectOverlay.model_validate(_example_document())

    assert overlay.publisher_login_env == "EXAMPLE_INSTALL_PUBLISHER_LOGIN"
    assert overlay.owed_quiet_seconds == 30


def test_an_overlay_without_publisher_login_env_refuses_the_load() -> None:
    document = _example_document()
    del document["publisher_login_env"]

    with pytest.raises(ValidationError, match="publisher_login_env"):
        ModelInstallRequestEffectOverlay.model_validate(document)


def test_an_overlay_with_a_blank_publisher_login_env_refuses_the_load() -> None:
    document = _example_document()
    document["publisher_login_env"] = ""

    with pytest.raises(ValidationError, match="publisher_login_env"):
        ModelInstallRequestEffectOverlay.model_validate(document)


def test_an_overlay_with_a_quiet_window_below_one_second_refuses_the_load() -> None:
    document = _example_document()
    document["owed_quiet_seconds"] = 0

    with pytest.raises(ValidationError, match="owed_quiet_seconds"):
        ModelInstallRequestEffectOverlay.model_validate(document)


def test_the_overlay_is_loaded_from_its_named_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "roots"
    folder = root / NODE_NAME
    folder.mkdir(parents=True)
    folder.joinpath("overlay.yaml").write_text(
        yaml.safe_dump(_example_document()), encoding="utf-8"
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))

    overlay = load_overlay()

    assert overlay.publisher_login_env == "EXAMPLE_INSTALL_PUBLISHER_LOGIN"
    assert overlay.owed_quiet_seconds == 30
