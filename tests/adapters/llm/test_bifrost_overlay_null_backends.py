# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Legacy null backend overlays inherit the committed delegation backends."""

from pathlib import Path

import pytest

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    deep_merge_bifrost_delegation_config,
    load_bifrost_delegation_config,
    load_bifrost_delegation_config_payload,
)
from omnimarket.handlers.routing.delegation_backend_resolution import (
    load_bifrost_backends,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("in_memory", [False, True])
def test_null_backends_preserve_valid_committed_config(
    tmp_path: Path, in_memory: bool
) -> None:
    contract = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/configs/bifrost_delegation.yaml"
    )
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("backends: null\n")
    expected = load_bifrost_delegation_config(config_path=contract)
    if in_memory:
        actual = load_bifrost_delegation_config_payload(
            contract.read_bytes(),
            overlay.read_bytes(),
            contract_source=str(contract),
            overlay_source=str(overlay),
        )
    else:
        actual = load_bifrost_delegation_config(
            config_path=contract, overlay_path=overlay
        )
    assert actual.backends == expected.backends
    assert actual.default_backends == expected.default_backends


def test_null_backends_do_not_change_other_null_merge_semantics() -> None:
    """Control: a null overlay key other than backends still clears its base value, as before the fix."""
    base = {"backends": [{"backend_id": "existing", "max_tokens": 64}], "other": 123}
    assert deep_merge_bifrost_delegation_config(
        base, {"backends": None, "other": None}
    ) == {"backends": [{"backend_id": "existing", "max_tokens": 64}], "other": None}
    assert base["other"] == 123


def test_nonempty_backend_overlay_still_updates_by_identity() -> None:
    """Control: a non-empty backends overlay still updates by backend_id, as before the fix."""
    assert deep_merge_bifrost_delegation_config(
        {"backends": [{"backend_id": "existing", "max_tokens": 64}]},
        {"backends": [{"backend_id": "existing", "max_tokens": 128}]},
    ) == {"backends": [{"backend_id": "existing", "max_tokens": 128}]}


def test_null_backends_in_delegation_routing_loader(tmp_path: Path) -> None:
    contract = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/configs/bifrost_delegation.yaml"
    )
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("backends: null\n")
    expected = load_bifrost_backends(config_path=contract)
    assert load_bifrost_backends(config_path=contract, overlay_path=overlay) == expected
