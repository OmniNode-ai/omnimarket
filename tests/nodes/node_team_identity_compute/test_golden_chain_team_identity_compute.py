# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node is registered, and its packaged contract invokes with a typed request (OMN-21382)."""

from __future__ import annotations

import importlib
import shutil
import tomllib
from importlib.metadata import entry_points
from pathlib import Path
from typing import get_type_hints

import pytest
import yaml
from pydantic import BaseModel

pytestmark = pytest.mark.unit

NODE = "node_team_identity_compute"
PACKAGE = f"omnimarket.nodes.{NODE}"
REPO = Path(__file__).resolve().parents[3]
VALID = REPO / "tests" / "fixtures" / "team_identity" / "valid.yaml"


def test_node_is_registered_as_an_onex_node() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["entry-points"]["onex.nodes"][NODE] == PACKAGE
    installed = {ep.name: ep.value for ep in entry_points(group="onex.nodes")}
    if NODE in installed:
        assert installed[NODE] == PACKAGE
    package = importlib.import_module(PACKAGE)
    assert package.NodeTeamIdentityCompute.contract_path.is_file()


def test_contract_golden_chain(tmp_path: Path) -> None:
    package = importlib.import_module(PACKAGE)
    assert package.__file__ is not None
    contract = yaml.safe_load(
        (Path(package.__file__).parent / "contract.yaml").read_text()
    )
    metadata = yaml.safe_load(
        (Path(package.__file__).parent / "metadata.yaml").read_text()
    )
    assert contract["name"] == metadata["name"] == NODE
    assert metadata["entry_points"]["onex.nodes"][NODE] == PACKAGE
    assert contract["lifecycle"] == "experimental"
    assert contract["metadata"]["related_tickets"] == ["OMN-21382"]
    binding = contract["handler"]
    handler_class = getattr(
        importlib.import_module(binding["module"]), binding["class"]
    )
    assert (
        contract["handler_routing"]["default_handler"]
        == binding["module"] + ":" + binding["class"]
    )

    def model(ref: str) -> type[BaseModel]:
        module, _, name = ref.rpartition(".")
        result = getattr(importlib.import_module(module), name)
        assert issubclass(result, BaseModel)
        assert result.model_config == {"frozen": True, "extra": "forbid"}
        return result

    request_class = model(binding["input_model"])
    result_class = model(binding["output_model"])
    hints = get_type_hints(handler_class.handle)
    assert hints["request"] is request_class
    assert hints["return"] is result_class
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}

    overlay = tmp_path / "team_identity" / "overlay.yaml"
    overlay.parent.mkdir()
    shutil.copyfile(VALID, overlay)
    result = handler_class().handle(
        request_class.model_validate(
            {
                "claims": [{"source": "github_auth", "value": "fixture-operator-gh"}],
                "overlay_roots": [str(tmp_path)],
            }
        )
    )
    assert isinstance(result, result_class)
    assert result.privileges == "operator"
    assert result.source is not None
    assert result.source.path == str(overlay)
