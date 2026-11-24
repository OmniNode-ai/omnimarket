# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Task 19: each store kind is registered under the ``onex.backends`` entry-point group and declares its own inputs."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest

from omnimarket.handlers.overlay_store.store_backend_registry import (
    StoreWiringRefusedError,
    backends_from_entry_points,
)

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
KINDS = ("store_in_memory", "store_postgres", "store_valkey")


def _declared() -> dict[str, str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    group: dict[str, str] = data["project"]["entry-points"]["onex.backends"]
    return {k: v for k, v in group.items() if k in KINDS}


def test_store_registration_each_kind_is_declared_under_onex_backends() -> None:
    assert sorted(_declared()) == sorted(KINDS)


@pytest.mark.parametrize("kind", KINDS)
def test_store_registration_the_entry_resolves_to_a_backend_of_that_kind(
    kind: str,
) -> None:
    module, _, attr = _declared()[kind].partition(":")
    backend = getattr(importlib.import_module(module), attr)()
    assert backend.spec.kind == kind


def test_store_registration_in_memory_declares_no_input_and_no_secret() -> None:
    module, _, attr = _declared()["store_in_memory"].partition(":")
    spec = getattr(importlib.import_module(module), attr)().spec
    assert spec.inputs == ()
    assert spec.secrets == ()


def test_store_registration_valkey_declares_every_connection_value_and_one_secret() -> (
    None
):
    module, _, attr = _declared()["store_valkey"].partition(":")
    spec = getattr(importlib.import_module(module), attr)().spec
    assert len(spec.inputs) == 5
    assert len(spec.secrets) == 1


class _FakeEntryPoint:
    def __init__(self, name: str, target: object) -> None:
        self.name = name
        self._target = target

    def load(self) -> object:
        return self._target


def test_store_registration_loader_reads_the_group_by_kind() -> None:
    module, _, attr = _declared()["store_in_memory"].partition(":")
    cls = getattr(importlib.import_module(module), attr)
    registry = backends_from_entry_points([_FakeEntryPoint("store_in_memory", cls)])
    assert sorted(registry) == ["store_in_memory"]


def test_store_registration_a_name_that_differs_from_the_backends_kind_refuses() -> (
    None
):
    module, _, attr = _declared()["store_in_memory"].partition(":")
    cls = getattr(importlib.import_module(module), attr)
    with pytest.raises(StoreWiringRefusedError, match="other_name"):
        backends_from_entry_points([_FakeEntryPoint("other_name", cls)])
