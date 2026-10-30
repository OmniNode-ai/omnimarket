# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node is registered, and its packaged contract invokes with a typed request."""

from __future__ import annotations

import importlib
import shutil
import tomllib
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, get_type_hints

import pytest
import yaml
from pydantic import BaseModel

pytestmark = pytest.mark.unit

NODE = "node_session_resume_compute"
PACKAGE = f"omnimarket.nodes.{NODE}"
REPO = Path(__file__).resolve().parents[3]
IDENTITY = REPO / "tests" / "fixtures" / "team_identity" / "valid.yaml"
LEDGER = REPO / "tests" / "fixtures" / "session_recovery" / "ROLLING_WORK_LEDGER.md"


def test_node_is_registered_as_an_onex_node() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["entry-points"]["onex.nodes"][NODE] == PACKAGE
    installed = {ep.name: ep.value for ep in entry_points(group="onex.nodes")}
    if NODE in installed:
        assert installed[NODE] == PACKAGE
    package = importlib.import_module(PACKAGE)
    assert package.NodeSessionResumeCompute.contract_path.is_file()


def _request(tmp_path: Path) -> dict[str, Any]:
    roots = tmp_path / "roots"
    (roots / "team_identity").mkdir(parents=True)
    shutil.copyfile(IDENTITY, roots / "team_identity" / "overlay.yaml")
    claims = [{"source": "github_auth", "value": "fixture-member-a-gh"}]
    if NODE == "node_session_resume_compute":
        return {
            "operation": "dangling",
            "ledger_path": str(LEDGER),
            "identity_claims": claims,
            "overlay_roots": [str(roots)],
            "since": "2026-10-29T00:00:00Z",
            "t0": "2026-10-29T09:00:00Z",
        }
    return {
        "operation": "write",
        "directory": str(tmp_path / "handoffs"),
        "identity_claims": claims,
        "overlay_roots": [str(roots)],
        "now": "2026-10-29T09:00:00Z",
        "write": {"lane": "sess-a"},
    }


def test_contract_golden_chain(tmp_path: Path) -> None:
    package = importlib.import_module(PACKAGE)
    assert package.__file__ is not None
    here = Path(package.__file__).parent
    contract = yaml.safe_load((here / "contract.yaml").read_text())
    metadata = yaml.safe_load((here / "metadata.yaml").read_text())
    assert contract["name"] == metadata["name"] == NODE
    assert metadata["entry_points"]["onex.nodes"][NODE] == PACKAGE
    assert contract["lifecycle"] == "experimental"
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

    result = handler_class().handle(request_class.model_validate(_request(tmp_path)))
    assert isinstance(result, result_class)
    assert result.status == "ok"
    assert result.scope.entry_id == "fixture-member-a"
    assert result.scope.privileges == "team"
