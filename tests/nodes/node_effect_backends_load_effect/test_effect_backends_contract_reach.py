# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every operation the effect-backends contract advertises is reachable from a local run (OMN-23114).

The two contracts route two operations, ``load_contracts`` (read the node folders of a directory) and
``effect_backends`` (judge them). The node ships no entry point, so a local run only ever reached
``effect_backends`` and ``load_contracts`` was advertised on a path nothing could take. The local
runtime dispatches each advertised operation from the contract's own routing table.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_effect_backends_load_effect import (
    runtime_effect_backends as runtime,
)
from omnimarket.nodes.node_effect_backends_load_effect.handlers.handler_effect_backends_loader import (
    HandlerEffectBackendsLoader,
)
from omnimarket.nodes.node_effect_backends_load_effect.models.model_effect_backends_load_request import (
    ModelEffectBackendsLoadRequest,
)
from omnimarket.nodes.node_effect_backends_load_effect.models.model_loaded_contracts import (
    ModelLoadedContracts,
)

pytestmark = pytest.mark.unit

NODES_DIR = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
CONTRACTS = (
    NODES_DIR / "node_effect_backends_check_compute" / "contract.yaml",
    NODES_DIR / "node_effect_backends_load_effect" / "contract.yaml",
)


def _advertised_operations() -> list[str]:
    operations: list[str] = []
    for contract in CONTRACTS:
        routing = yaml.safe_load(contract.read_text(encoding="utf-8"))[
            "handler_routing"
        ]
        operations.extend(entry["operation"] for entry in routing["handlers"])
    return operations


def test_contracts_advertise_both_operations() -> None:
    assert _advertised_operations() == ["effect_backends", "load_contracts"]


def test_local_dispatch_table_holds_every_advertised_operation() -> None:
    assert sorted(runtime.dispatch_table(CONTRACTS)) == sorted(_advertised_operations())


def test_local_run_takes_the_load_contracts_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = tmp_path / "node_x_effect"
    node.mkdir()
    (node / "contract.yaml").write_text(
        yaml.safe_dump({"node_name": "node_x_effect", "node_type": "effect"})
    )
    seen: list[str] = []
    real_handle = HandlerEffectBackendsLoader.handle

    def spy(
        self: HandlerEffectBackendsLoader, request: ModelEffectBackendsLoadRequest
    ) -> ModelLoadedContracts:
        seen.append(request.root)
        return real_handle(self, request)

    monkeypatch.setattr(HandlerEffectBackendsLoader, "handle", spy)
    report = runtime.run_local(str(tmp_path), CONTRACTS)
    assert seen == [str(tmp_path)]
    assert report.without_in_memory_default == ("node_x_effect",)


def test_local_run_on_unavailable_root_reports_unavailable(tmp_path: Path) -> None:
    report = runtime.run_local(str(tmp_path / "absent"), CONTRACTS)
    assert report.inventory_available is False
    assert (
        runtime.main(
            [
                "--contract",
                str(CONTRACTS[0]),
                "--contract",
                str(CONTRACTS[1]),
                str(tmp_path / "absent"),
            ]
        )
        == 2
    )
