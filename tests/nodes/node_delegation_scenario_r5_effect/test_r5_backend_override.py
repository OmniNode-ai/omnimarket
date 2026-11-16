# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The unit's environment file may override the contract's model backend for one run (OMN-23555).

The contract ships ``model_backend: inmemory`` so a plain unit run is scripted, and only the environment file the
systemd unit loads can name ``real`` through ``ONEX_SCENARIO_R5_MODEL_BACKEND``: a scheduled tick then proves the real
chain instead of drawing a scripted pass. ``build_runner`` is the only reader of the variable — unset keeps the
contract's value, ``real`` / ``inmemory`` is honoured for that run and any other value refuses the tick. Building the
handler touches no network: the real backend's factory is chosen, never called.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_delegation_scenario_r5_effect.handlers.handler_scenario_r5 import (
    HandlerScenarioR5,
    build_runner,
    load_runtime,
)

pytestmark = pytest.mark.unit

RUNTIME = load_runtime()


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    environment = {
        RUNTIME.home_env: str(tmp_path),
        RUNTIME.ledger_append_command_env: "true x",
    }
    environment.update(extra)
    return environment


def _model_backend(handler: HandlerScenarioR5) -> str:
    # The same runtime the handler's own ``model_backend == "real"`` check reads; observable without a network.
    return handler._r5.model_backend


def test_backend_override_unset_keeps_the_contract_value(tmp_path: Path) -> None:
    handler = build_runner(_env(tmp_path))

    assert _model_backend(handler) == "inmemory"
    assert _model_backend(handler) == load_runtime().model_backend


def test_backend_override_real_selects_the_real_backend(tmp_path: Path) -> None:
    contract_default = load_runtime().model_backend
    # Control: the contract itself stays scripted, so without the override path this assertion could not hold.
    assert contract_default == "inmemory"

    handler = build_runner(_env(tmp_path, **{RUNTIME.model_backend_env: "real"}))

    assert _model_backend(handler) == "real"
    assert _model_backend(handler) != contract_default


def test_backend_override_bogus_refuses_to_build_the_runner(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match=RUNTIME.model_backend_env):
        build_runner(_env(tmp_path, **{RUNTIME.model_backend_env: "bogus"}))
