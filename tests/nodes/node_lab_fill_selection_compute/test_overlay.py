# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Strict validation of parsed deployment overlay mappings."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from omnimarket.nodes.node_lab_fill_selection_compute.models import (
    ModelLabFillDeployment,
)

ROLES = (
    "companion_repositories",
    "not_work_repositories",
    "document_repositories",
)
OVERLAY = {
    "companion_repositories": ["change_control"],
    "not_work_repositories": ["registry"],
    "document_repositories": ["docs", "docs_private"],
}


def test_from_overlay_valid_mapping() -> None:
    assert ModelLabFillDeployment.from_overlay(OVERLAY) == ModelLabFillDeployment(
        ("change_control",), ("registry",), ("docs", "docs_private")
    )
    assert ModelLabFillDeployment.from_overlay(
        {key: [] for key in ROLES}
    ) == ModelLabFillDeployment((), (), ())


@pytest.mark.parametrize("key", ROLES)
def test_from_overlay_missing_key(key: str) -> None:
    with pytest.raises(ValueError, match=f"missing keys: {key}"):
        ModelLabFillDeployment.from_overlay(
            {name: value for name, value in OVERLAY.items() if name != key}
        )


def test_from_overlay_unknown_key() -> None:
    with pytest.raises(ValueError, match="unknown keys: unexpected"):
        ModelLabFillDeployment.from_overlay({**OVERLAY, "unexpected": []})


@pytest.mark.parametrize("key", ROLES)
@pytest.mark.parametrize("value", ["repo_a", ("repo_a",), None, True, {}])
def test_from_overlay_non_list(key: str, value: object) -> None:
    with pytest.raises(ValueError, match=f"{key} must be a list of strings"):
        ModelLabFillDeployment.from_overlay({**OVERLAY, key: value})


@pytest.mark.parametrize("key", ROLES)
@pytest.mark.parametrize("item", [7, None, True, ["repo_a"], {}])
def test_from_overlay_non_str_item(key: str, item: object) -> None:
    with pytest.raises(ValueError, match=rf"{key}\[1\] must be a string"):
        ModelLabFillDeployment.from_overlay({**OVERLAY, key: ["repo_a", item]})


@pytest.mark.parametrize("raw", [None, [], "mapping", True, 7])
def test_from_overlay_non_dict(raw: object) -> None:
    with pytest.raises(ValueError, match="overlay must be a dict mapping"):
        ModelLabFillDeployment.from_overlay(raw)


def test_deployment_is_frozen() -> None:
    deployment = ModelLabFillDeployment.from_overlay(OVERLAY)
    with pytest.raises(FrozenInstanceError):
        deployment.companion_repositories = ()


@pytest.mark.parametrize("value", ["repo_a", (7,), ["repo_a"]])
def test_injected_deployment_types_are_validated(value: object) -> None:
    with pytest.raises(ValueError, match="companion_repositories"):
        ModelLabFillDeployment(
            **{
                "companion_repositories": value,
                "not_work_repositories": (),
                "document_repositories": (),
            }
        )
