# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-22198 - the omnibase-spi pin carries the lane host protocols.

The remote lane start nodes take their host seams from omnibase_spi
(``ProtocolLaneHostTransport``, ``ProtocolLaneHostPool`` and
``ProtocolWorkLedgerStore``), so the pinned spi tag has to contain them. The
pinned tag also carries the ``ProtocolSecretStore.close`` signature that every
secret store of this repo implements.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

LANE_PROTOCOLS = (
    "ProtocolLaneHostTransport",
    "ProtocolLaneHostPool",
    "ProtocolWorkLedgerStore",
)
TRANSPORT_OPERATIONS = (
    "prepare_worktree",
    "start_engine_detached",
    "read_engine_exit",
    "stop_engine",
    "read_log_tail",
)


@pytest.mark.parametrize("name", LANE_PROTOCOLS)
def test_spi_pin_carries_lane_protocol(name: str) -> None:
    module = importlib.import_module("omnibase_spi.protocols.lane")
    protocol = getattr(module, name)
    assert protocol.__module__.startswith("omnibase_spi."), name


def test_spi_pin_lane_host_transport_operations() -> None:
    module = importlib.import_module("omnibase_spi.protocols.lane")
    transport = module.ProtocolLaneHostTransport
    missing = [op for op in TRANSPORT_OPERATIONS if not hasattr(transport, op)]
    assert missing == []


def test_spi_pin_secret_store_close_takes_optional_timeout() -> None:
    from omnibase_spi.protocols.services.protocol_secret_store import (
        ProtocolSecretStore,
    )

    parameter = inspect.signature(ProtocolSecretStore.close).parameters[
        "timeout_seconds"
    ]
    assert parameter.default is None


def test_secret_store_implementations_accept_optional_close_timeout() -> None:
    """Require every secret store close timeout to default to None."""
    repo_root = Path(__file__).resolve().parents[2]
    paths = (
        "src/omnimarket/cli/cli_secret.py",
        "src/omnimarket/inference/local_byok_credential_adapter.py",
        "src/omnimarket/inference/secret_store_resolver.py",
        "src/omnimarket/nodes/node_secret_store_effect/adapters/identity_file.py",
        "src/omnimarket/nodes/node_secret_store_effect/adapters/local_database_store.py",
        "src/omnimarket/nodes/node_secret_store_effect/handlers/delegation_key_store.py",
    )
    offenders: list[str] = []
    for path in paths:
        tree = ast.parse((repo_root / path).read_text(encoding="utf-8"))
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef) or node.name != "close":
                continue
            parameters = node.args.args + node.args.kwonlyargs
            defaults = (
                [None] * (len(node.args.args) - len(node.args.defaults))
                + node.args.defaults
                + node.args.kw_defaults
            )
            for parameter, default in zip(parameters, defaults, strict=True):
                if parameter.arg != "timeout_seconds":
                    continue
                found = True
                if not (isinstance(default, ast.Constant) and default.value is None):
                    offenders.append(f"{path}:{node.lineno}")
        if not found:
            offenders.append(f"{path}:no close(timeout_seconds)")
    assert offenders == []
