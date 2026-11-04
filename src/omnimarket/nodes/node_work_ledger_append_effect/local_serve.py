# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger host's local wiring of the append handler (OMN-22185).

The handler reaches files only through its protocols. This module is where the
local adapters meet the contract: the ``append_if_absent`` section names the id
cell, the lock-file suffix beside the ledger and the lock wait, and an absent key
refuses by name, so none of them is a constant in code.
"""

from collections.abc import Iterable, Mapping
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.work_ledger_append import ModelAppendIfAbsentPolicy
from omnimarket.nodes.node_work_ledger_append_effect.handlers.handler_work_ledger_append_effect import (
    HandlerWorkLedgerAppendEffect,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    LocalLedgerAppendLock,
    LocalLedgerFile,
    ProtocolLedgerAppendRunner,
)

_NODE = "node_work_ledger_append_effect"
_SECTION = "append_if_absent"


def load_append_if_absent_policy(
    contract: Mapping[str, Any] | None = None,
) -> ModelAppendIfAbsentPolicy:
    """The contract's ``append_if_absent`` section; an absent section or key refuses naming it."""
    if contract is None:
        contract = yaml.safe_load(
            resources.files(f"omnimarket.nodes.{_NODE}")
            .joinpath("contract.yaml")
            .read_text()
        )
    section = contract.get(_SECTION) if contract else None
    if not isinstance(section, dict):
        raise ValueError(f"{_NODE} contract declares no {_SECTION} section")
    for key in ModelAppendIfAbsentPolicy.model_fields:
        if key not in section:
            raise ValueError(f"{_NODE} contract {_SECTION}.{key} is absent")
    return ModelAppendIfAbsentPolicy(**section)


def local_serve_handler(
    runner: ProtocolLedgerAppendRunner,
    ledger: Path,
    host_name: str,
    writer_principals: Iterable[str],
    operator_principal: str | None,
) -> HandlerWorkLedgerAppendEffect:
    """The handler on a local ledger file, its check-then-append step under a flock beside it."""
    policy = load_append_if_absent_policy()
    return HandlerWorkLedgerAppendEffect(
        runner,
        LocalLedgerFile(ledger),
        host_name,
        writer_principals=writer_principals,
        operator_principal=operator_principal,
        append_lock=LocalLedgerAppendLock(
            ledger.with_name(ledger.name + policy.lock_suffix),
            policy.lock_wait_seconds,
        ),
        append_if_absent=policy,
    )
