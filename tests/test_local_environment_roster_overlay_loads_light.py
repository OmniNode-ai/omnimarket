# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane roster model loads with only pydantic and pyyaml installed.

The private overlays repository validates each overlay through its node's model in an environment holding pydantic,
pydantic-settings and pyyaml, and nothing else. The roster model sat in the handler module, which imports an HTTP client and
the infrastructure and core libraries, so the check could not load it. The models now live in the node's ``overlay`` module.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_local_environment_observe_effect import (
    overlay as roster_overlay,
)
from omnimarket.nodes.node_local_environment_observe_effect.handlers import (
    handler_local_environment_observe as observer,
)

_NODE = "node_local_environment_observe_effect"

_LIGHT_LOAD = """
import sys

class _Absent:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in {"httpx", "omnibase_core", "omnibase_infra"}:
            raise ImportError(f"{name} is not installed in the overlay check")
        return None

sys.meta_path.insert(0, _Absent())
from omnimarket.nodes.node_local_environment_observe_effect.overlay import ModelLocalEnvironmentRoster

roster = ModelLocalEnvironmentRoster.model_validate(
    {"services": [{"service": "runtime", "port": 8085, "scheme": "http", "path": "/health"}]}
)
print(len(roster.services))
"""


def _write(root: Path, text: str) -> Path:
    target = root / _NODE
    target.mkdir(parents=True)
    (target / "overlay.yaml").write_text(text)
    return root


def test_roster_overlay_loads_through_the_overlay_module_models(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        "services:\n"
        "  - service: omninode-runtime\n    port: 8085\n    scheme: http\n    path: /health\n"
        "  - service: postgres\n    port: 5432\n",
    )
    roster = load_structured_overlay(
        _NODE,
        roster_overlay.ModelLocalEnvironmentRoster,
        {"ONEX_SKILL_OVERLAY_ROOTS": str(root)},
    )
    assert [s.service for s in roster.services] == ["omninode-runtime", "postgres"]
    assert roster.services[1].scheme is None


def test_the_handler_names_the_same_models_as_the_overlay_module() -> None:
    assert (
        observer.ModelLocalEnvironmentRoster
        is roster_overlay.ModelLocalEnvironmentRoster
    )
    assert observer.ModelProbeTarget is roster_overlay.ModelProbeTarget


def test_an_empty_roster_is_refused() -> None:
    with pytest.raises(ValidationError):
        roster_overlay.ModelLocalEnvironmentRoster.model_validate({"services": []})


def test_an_entry_with_an_unknown_key_is_refused() -> None:
    with pytest.raises(ValidationError):
        roster_overlay.ModelLocalEnvironmentRoster.model_validate(
            {"services": [{"service": "runtime", "port": 8085, "bogus": 1}]}
        )


def test_the_roster_model_loads_where_the_handlers_packages_are_absent() -> None:
    done = subprocess.run(
        [sys.executable, "-c", _LIGHT_LOAD],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-800:]
    assert done.stdout.strip() == "1"
