# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node's overlay model loads the shipped example, refuses gaps and answers lookups (OMN-24242).

Nothing in the overlay has a default: a duplicated host, an absent key and a lookup of an
undeclared host are each refused, and each refusal names what was wrong.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_host_local_config_drift_compute.overlay import (
    ModelHostLocalConfigDriftOverlay,
    declaration_for,
)
from tests.nodes.node_host_local_config_drift_compute.support import (
    HOST,
    declaration,
)

pytestmark = pytest.mark.unit

_EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_host_local_config_drift_compute"
    / "overlay.example.yaml"
)
_INJECTED_FAULT = "Leave one backup copy more than the declared bound"


def _overlay(**overrides: object) -> ModelHostLocalConfigDriftOverlay:
    fields: dict[str, object] = {
        "hosts": (declaration(),),
        "injected_fault": _INJECTED_FAULT,
    }
    fields.update(overrides)
    return ModelHostLocalConfigDriftOverlay(**fields)


def test_the_shipped_example_overlay_loads() -> None:
    overlay = ModelHostLocalConfigDriftOverlay.model_validate(
        yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    )

    assert [host.host for host in overlay.hosts] == ["host-a"]
    assert overlay.injected_fault

    found = declaration_for(overlay, "host-a")
    assert [alias.alias for alias in found.ssh_aliases] == ["host-b"]
    assert found.git_allowed_local_keys == ("core.hookspath",)
    assert found.env_dirs[0].directory == "~/.example"
    assert found.env_dirs[0].live_files == (".env",)
    assert found.env_dirs[0].max_backups == 2


def test_an_overlay_with_a_duplicate_host_name_is_refused() -> None:
    with pytest.raises(ValidationError, match="a host is declared twice"):
        _overlay(hosts=(declaration(), declaration()))


def test_an_absent_key_is_refused_naming_it() -> None:
    document = yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    del document["injected_fault"]

    with pytest.raises(ValidationError) as exc:
        ModelHostLocalConfigDriftOverlay.model_validate(document)

    assert ("injected_fault",) in [error["loc"] for error in exc.value.errors()]


def test_declaration_for_an_absent_host_raises_key_error() -> None:
    overlay = _overlay()
    assert declaration_for(overlay, HOST) is overlay.hosts[0]

    with pytest.raises(KeyError, match="is not declared by the overlay") as exc:
        declaration_for(overlay, "host-absent")

    assert "host-absent" in str(exc.value)
